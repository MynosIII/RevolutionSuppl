# app.py

import base64
import io
import os
import re
from datetime import datetime

import numpy as np
import matplotlib
import matplotlib.pyplot as plt
import pandas as pd
import requests
import seaborn as sns
from flask import (Flask, Response, flash, redirect, render_template, request,
                   send_file, url_for)
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
from numpy.polynomial.polynomial import Polynomial
from PIL import Image
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# =============================================================================
# --- CONFIGURACIÓN DE FLASK Y CONSTANTES ---
# =============================================================================

app = Flask(__name__)
# Se necesita una SECRET_KEY para usar `flash`
app.config['SECRET_KEY'] = 'tu-clave-secreta-aqui' 
# Directorio temporal para archivos subidos si es necesario
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# [cite_start]Constantes del script original [cite: 3]
CONSTANTS = {
    'TEXT_COLUMNS_TO_DROP': [
        'Buy Box', 'Category', 'Size Tier', 'Fulfillment', 'Dimensions',
        'ABA Most Clicked', 'Seller Country/Region', 'Brand', 'Image URL',
        'URL', 'ASIN', 'Product Details', 'Sponsored', 'Best Seller',
        'Creation Date', 'Seller'
                            ],
    'FINANCIAL_DATA_REGEX': {
        'revenue': r"Sales\s*\$ ([\d,]+\.\d{2})",
        'net_profit': r"Net profit\s*\$ ([\d,]+\.\d{2})",
        'adspend': r"Advertising cost\s*-\$ ([\d,]+\.\d{2})",
        '% Refunds': r"Margin\s*([\d\.]+)%",
        'refund_cost': r"Refund сost\s*-\$ ([\d,]+\.\d{2})",
        'units': r"\+Units\s*(\d+)",
        'Organic Units': r"\+Organic\s*(\d+)",
        'PPC Units': r"\+Sponsored Products (same day)\s*(\d+)",
        'sessions': r"\+Sessions\s*(\d+)",
        'mobile sessions': r"\+Mobile app sessions\s*(\d+)",
        'browser sessions': r"\+Browser sessions\s*(\d+)",
        'margin': r"Margin\s*([\d\.]+)%",
        'amazon_fees': r"Amazon fees\s*-\$ ([\d,]+\.\d{2})",
        'Unit session percentage': r"Unit session percentage\s*([\d\.]+)%"
                            },
}

# =============================================================================
# --- FUNCIONES DE PROCESAMIENTO DE DATOS (REUTILIZADAS) ---
# =============================================================================

def load_data_from_filestorage(file_storage):
    """Carga datos desde un objeto FileStorage de Flask."""
    try:
        if file_storage.filename.lower().endswith('.csv'):
            return pd.read_csv(file_storage)
        elif file_storage.filename.lower().endswith(('.xlsx', '.xls')):
            return pd.read_excel(file_storage)
        else:
            return None
    except Exception as e:
        flash(f"Error al cargar el archivo {file_storage.filename}: {e}", "danger")
        return None

# Todas las siguientes funciones son adaptadas del script original
def clean_numeric_columns(df): # [cite: 6]
    for col in df.select_dtypes(include=['object']).columns:
        if pd.api.types.is_string_dtype(df[col]):
            try:
                df[col] = pd.to_numeric(df[col].str.replace(',', '', regex=False), errors='coerce')
            except AttributeError:
                continue
    return df

def process_creation_date(df): # [cite: 8]
    if 'Creation Date' in df.columns:
        def convert_date(date_str):
            if pd.isna(date_str): return pd.NaT
            try: return pd.to_datetime(date_str)
            except (ValueError, TypeError):
                try: return pd.to_datetime(datetime.strptime(str(date_str), "%b %d, %Y"))
                except (ValueError, TypeError): return pd.NaT
        df['Creation Date'] = df['Creation Date'].apply(convert_date)
        df['Days Since Creation'] = (pd.to_datetime('today') - df['Creation Date']).dt.days
    return df

def drop_text_columns(df): # [cite: 11]
    cols_to_drop = [col for col in CONSTANTS['TEXT_COLUMNS_TO_DROP'] if col in df.columns]
    return df.drop(columns=cols_to_drop, errors='ignore')

def _calculate_white_percentage(image_url): # [cite: 54]
    if not isinstance(image_url, str) or not image_url.startswith('http'): return None
    try:
        response = requests.get(image_url, timeout=10)
        response.raise_for_status()
        img = Image.open(io.BytesIO(response.content)).convert("RGB")
        pixels = np.array(img)
        white_pixels = np.sum(np.all(pixels >= [250, 250, 250], axis=-1))
        total_pixels = pixels.shape[0] * pixels.shape[1]
        return (white_pixels / total_pixels) * 100 if total_pixels > 0 else 0
    except requests.exceptions.RequestException:
        return None
    except Exception:
        return None

# =============================================================================
# --- RUTAS PRINCIPALES Y MENÚ ---
# =============================================================================

@app.route('/')
def index():
    """Página de inicio que muestra el menú principal."""
    return render_template('index.html')

# =============================================================================
# --- HERRAMIENTA: FILE MERGER ---
# =============================================================================

@app.route('/merger', methods=['GET', 'POST'])
def merger():
    """Fusiona múltiples archivos CSV/Excel en uno solo."""
    if request.method == 'POST':
        files = request.files.getlist('files')
        if not files or all(f.filename == '' for f in files):
            flash('No files were selected. Please upload at least one file.', 'warning')
            return redirect(request.url)

        output_buffer = io.BytesIO()
        with pd.ExcelWriter(output_buffer, engine='openpyxl') as writer:
            for file in files:
                if file:
                    df = load_data_from_filestorage(file)
                    if df is not None:
                        sheet_name = os.path.splitext(os.path.basename(file.filename))[0][:31]
                        df.to_excel(writer, sheet_name=sheet_name, index=False)
        
        output_buffer.seek(0)
        return send_file(
            output_buffer,
            as_attachment=True,
            download_name='merged_files.xlsx',
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )

    return render_template('merger.html')

# =============================================================================
# --- HERRAMIENTA: FINANCIAL CHECKER ---
# =============================================================================

@app.route('/financial-checker', methods=['GET', 'POST'])
def financial_checker():
    """Analiza un bloque de texto con datos financieros."""
    if request.method == 'POST':
        text = request.form.get('financial_text', '').strip()
        if not text:
            flash('The text area is empty. Please paste some data.', 'warning')
            return redirect(request.url)

        data = {}
        for key, regex in CONSTANTS['FINANCIAL_DATA_REGEX'].items():
            match = re.search(regex, text)
            if match:
                value_str = match.group(1).replace(',', '')
                data[key] = float(value_str) if '.' in value_str else int(value_str)
            else:
                data[key] = 0
        
        revenue = data.get('revenue', 0)
        units = data.get('units', 1) 
        net_profit = data.get('net_profit', 0)
        adspend = data.get('adspend', 0)
        refund_cost = data.get('refund_cost', 0)

        data['organic_units'] = int(data.get('units', 0) * 0.7)
        data['ppc_units'] = data.get('units', 0) - data['organic_units']
        
        if revenue > 0 and units > 0:
             data['expected_cm_no_ads'] = (net_profit + abs(adspend)) / revenue * ((revenue / units) * data['organic_units'])
        else:
            data['expected_cm_no_ads'] = 0

        preadmargin = round((net_profit + abs(adspend)) / revenue * 100, 2) if revenue else 0
        expected_cm_no_returns = round(net_profit + abs(refund_cost), 2)
        expected_margin_no_returns = round((expected_cm_no_returns / revenue) * 100, 2) if revenue else 0
        
        results = {
            "Revenue": f"${revenue:,.2f}", "Margin": f"{data.get('margin', 0)}%",
            "Contribution Margin": f"${net_profit:,.2f}", "AdSpend": f"${adspend:,.2f}",
            "Pre-Ad Margin": f"{preadmargin:.2f}%",
            "Expected CM (No Ads)": f"${data.get('expected_cm_no_ads', 0):,.2f}",
            "--- Units ---": "---", "Total Units": units, "Organic Units": data.get('organic_units', 0),
            "PPC Units": data.get('ppc_units', 0), "--- Refunds ---": "---",
            "Refund Rate": f"{data.get('% Refunds', 0)}%",
            "Expected CM (No Returns)": f"${expected_cm_no_returns:,.2f}",
            "Expected Margin (No Returns)": f"{expected_margin_no_returns:.2f}%",
            "--- Other Metrics ---": "---", "Amazon Fees": f"${data.get('amazon_fees', 0):,.2f}",
            "Amazon Fees / Unit": f"${data.get('amazon_fees', 0) / units:,.2f}" if units else "$0.00",
            "Sessions": f"{data.get('sessions',0):,}",
            "Conversion Rate": f"{data.get('Unit session percentage', 0)}%",
        }
        
        return render_template('financial_checker.html', results=results, original_text=text)

    return render_template('financial_checker.html')


# =============================================================================
# --- HERRAMIENTA: XRAY ANALYSIS ---
# =============================================================================

def generate_plot_base64(fig):
    """Convert a Matplotlib figure to a Base64 image."""
    buf = io.BytesIO()
    FigureCanvas(fig).print_png(buf)
    return base64.b64encode(buf.getvalue()).decode('utf-8')

@app.route('/xray', methods=['GET', 'POST'])
def xray_analysis():
    """File upload page for XRay analysis."""
    if request.method == 'POST':
        file = request.files.get('file')
        if not file or file.filename == '':
            flash('No file was selected', 'warning')
            return redirect(request.url)
        
        df = load_data_from_filestorage(file)
        if df is None:
            return redirect(request.url)

        # [cite_start]Preparar datos [cite: 33, 34]
        df_cleaned = df.copy()
        df_cleaned = clean_numeric_columns(df_cleaned)
        df_cleaned = process_creation_date(df_cleaned)
        df_cleaned = drop_text_columns(df_cleaned)
        df_cleaned = df_cleaned.drop_duplicates()

        plots = {}

        # [cite_start]1. Matriz de Correlación [cite: 35, 36]
        numerical_df = df_cleaned.select_dtypes(include=np.number)
        if numerical_df.shape[1] >= 2:
            fig_corr, ax_corr = plt.subplots(figsize=(12, 10))
            sns.heatmap(numerical_df.corr(), annot=True, cmap='coolwarm', fmt='.2f', linewidths=0.5, ax=ax_corr)
            ax_corr.set_title('Correlation Heatmap', fontsize=16)
            plt.tight_layout()
            plots['correlation_matrix'] = generate_plot_base64(fig_corr)
            plt.close(fig_corr)

        # [cite_start]2. Gráfico de Precio vs. Ventas [cite: 38, 39, 40, 41]
        price_col = next((col for col in df.columns if 'price' in col.lower()), None)
        sales_col = next((col for col in df.columns if 'sales' in col.lower()), None)
        if price_col and sales_col:
            data = df.drop_duplicates(subset=['ASIN']).copy()
            data[price_col] = pd.to_numeric(data[price_col].astype(str).str.replace(r'[$,]', '', regex=True), errors='coerce').fillna(0)
            data[sales_col] = pd.to_numeric(data[sales_col].astype(str).str.replace(r'[,]', '', regex=True), errors='coerce').fillna(0)
            data = data[(data[price_col] > 0) & (data[sales_col] > 0)]
            if not data.empty:
                fig_price, ax_price = plt.subplots(figsize=(10, 7))
                poly_fit = Polynomial.fit(data[price_col], data[sales_col], deg=2)
                x_smooth = np.linspace(data[price_col].min(), data[price_col].max(), 400)
                y_smooth = np.maximum(0, poly_fit(x_smooth))
                ax_price.scatter(data[price_col], data[sales_col], alpha=0.6, label='Competitor Data')
                ax_price.plot(x_smooth, y_smooth, color='red', linestyle='--', label='Sales Trendline')
                # [cite_start]Estadísticas clave [cite: 44, 45]
                avg_price = data[price_col].mean()
                median_price = data[price_col].median()
                price_max_sales = data.loc[data[sales_col].idxmax()][price_col]
                ax_price.axvline(avg_price, color='green', linestyle=':', label=f'Avg Price: ${avg_price:.2f}')
                ax_price.axvline(median_price, color='orange', linestyle=':', label=f'Median Price: ${median_price:.2f}')
                ax_price.axvline(price_max_sales, color='purple', linestyle=':', label=f'Price for Max Sales: ${price_max_sales:.2f}')
                ax_price.set_title('Price vs. Sales Distribution'); ax_price.set_xlabel('Price ($)'); ax_price.set_ylabel('Monthly Sales (Units)')
                ax_price.legend(); ax_price.grid(True, linestyle='--', alpha=0.6)
                plt.tight_layout()
                plots['price_vs_sales'] = generate_plot_base64(fig_price)
                plt.close(fig_price)

        # [cite_start]3. Análisis de Imagen [cite: 47, 48, 49, 50, 51]
        if 'ASIN' in df.columns and 'Image URL' in df.columns and sales_col:
            img_data = df.drop_duplicates(subset=['ASIN']).copy()
            results = []
            for _, row in img_data.iterrows():
                white_perc = _calculate_white_percentage(row['Image URL'])
                if white_perc is not None:
                    results.append([row['ASIN'], white_perc, row[sales_col]])
            if results:
                results_df = pd.DataFrame(results, columns=['ASIN', 'White Percentage', 'Sales'])
                results_df['Sales'] = pd.to_numeric(results_df['Sales'].astype(str).str.replace(r'[,]', '', regex=True), errors='coerce')
                results_df.dropna(inplace=True)
                if not results_df.empty:
                    fig_img, ax_img = plt.subplots(figsize=(10, 6))
                    sns.regplot(data=results_df, x='White Percentage', y='Sales', order=2, ci=None, line_kws={'color':'red', 'linestyle':'--'}, scatter_kws={'alpha':0.6}, ax=ax_img) # [cite: 53]
                    ax_img.set_title('Sales vs. Main Image White Space Percentage')
                    ax_img.set_xlabel('White Percentage (%)'); ax_img.set_ylabel('Sales (Units)')
                    ax_img.grid(True)
                    plt.tight_layout()
                    plots['image_analysis'] = generate_plot_base64(fig_img)
                    plt.close(fig_img)

        if not plots:
            flash('Graphs could not be generated. Please check that the file contains enough numerical data and the correct columns (price, sales, Image URL)', 'info')
            return redirect(request.url)

        return render_template('xray_results.html', plots=plots)

    return render_template('xray_analysis.html')

# =============================================================================
# --- HERRAMIENTA: AMAZON FIT CHECKER (CON STREAMING) ---
# =============================================================================

def stream_fit_checker(asins):
    """Selenium powered tool executes this analysis in real time"""
    yield f"🔍Starting the search for {len(asins)} ASIN(s)...\n\n"
    
    # [cite_start]Configuración de Selenium [cite: 63, 64]
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920x1080")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    
    driver = None
    try:
        # Intenta usar un Service object para mayor compatibilidad
        from selenium.webdriver.chrome.service import Service as ChromeService
        from webdriver_manager.chrome import ChromeDriverManager
        driver = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=chrome_options)
    except ImportError: # Fallback para instalaciones más simples
        driver = webdriver.Chrome(options=chrome_options)
    except Exception as e:
        yield f"ERROR: selenium driver was not found. please contact support Error: {e}\n"
        return

    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
         "source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined })"
    })

    for asin in asins:
        url = f"https://www.amazon.com/dp/{asin}"
        try:
            driver.get(url)
            try:
                WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, "//input[@data-action-type='DISMISS']"))).click()
                yield f"{asin}: Pop-up Location discarded. Please do not continue checking on this PC for long!\n"
            except TimeoutException:
                pass 

            # Esperar el elemento objetivo
            id_busqueda = "automotive-pf-primary-view"
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CLASS_NAME, id_busqueda)))
            yield f"{asin}: ✅ Elemento Found (class): '{id_busqueda}'\n"
        except TimeoutException:
            yield f"{asin}: ❌ Element NOT Found (class): '{id_busqueda}'\n"
        except Exception as e:
            yield f"{asin}: ⚠️ Unexpected Error: {e}\n"
    
    driver.quit()
    yield "\n✅ Process completed."

@app.route('/fit-checker', methods=['GET', 'POST'])
def fit_checker():
    """Web with the form for the ASINs"""
    if request.method == 'POST':
        asins_input = request.form.get('asins', '')
        asins = [x.strip() for x in asins_input.replace(",", " ").split() if x.strip()]
        if not asins:
            flash('Please input at least 1 ASIN', 'warning')
            return redirect(request.url)
        
        # El streaming se manejará con JavaScript en el frontend
        return Response(stream_fit_checker(asins), mimetype='text/plain')

    return render_template('fit_checker.html')


# =============================================================================
# --- INICIO DE LA APLICACIÓN ---
# =============================================================================

if __name__ == '__main__':
    # Usar debug=True solo para desarrollo
    app.run(debug=True, host='0.0.0.0', port=5001)