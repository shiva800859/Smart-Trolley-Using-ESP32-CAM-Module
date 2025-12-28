import cv2
import numpy as np
import pyzbar.pyzbar as pyzbar
import urllib.request
from flask import Flask, render_template, jsonify, request
import threading
import time
import sqlite3
import json
from datetime import datetime
import configparser
import os

app = Flask(__name__)

# --- paths ---
ROOT_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(ROOT_DIR, 'config.ini')
PRODUCTS_JSON_PATH = os.path.join(ROOT_DIR, 'products.json')

# --- load config.ini (general settings) ---
config = configparser.ConfigParser()
config.optionxform = str  # keep case for other keys if needed
if not os.path.exists(CONFIG_PATH):
    raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")
config.read(CONFIG_PATH)

general = config['general']
IP_CAMERA_URL = general.get('ip_camera_url', 'http://192.168.0.155/')
CAMERA_FRAME_SUFFIX = general.get('camera_frame_url_suffix', 'cam-hi.jpg')
USE_WEBCAM = general.getboolean('use_webcam', fallback=False)
WEBCAM_INDEX = general.getint('webcam_index', fallback=0)
DB_PATH = general.get('db_path', 'trolley.db')
CURRENCY_SYMBOL = general.get('currency_symbol', '\u20b9').encode('utf-8').decode('unicode_escape')
CURRENCY_MULTIPLIER = general.getfloat('currency_multiplier', fallback=1.0)
RESCAN_DELAY = general.getfloat('rescan_delay_seconds', fallback=2.0)
FLASK_HOST = general.get('host', '0.0.0.0')
FLASK_PORT = general.getint('port', fallback=5000)
FLASK_DEBUG = general.getboolean('debug', fallback=False)

# --- product database (loaded from products.json) ---
product_database = {}

def load_products_from_json(path=PRODUCTS_JSON_PATH):
    global product_database
    if not os.path.exists(path):
        print(f"Products file not found: {path}. Using empty product list.")
        product_database = {}
        return

    with open(path, 'r', encoding='utf-8') as f:
        try:
            raw = json.load(f)
        except Exception as e:
            print(f"Failed to parse products JSON: {e}")
            product_database = {}
            return

    # Normalize barcodes to UPPERCASE to ensure matching independent of case
    product_database = {}
    for barcode, info in raw.items():
        if not barcode:
            continue
        bc = barcode.strip().upper()
        name = info.get('name', '').strip() if isinstance(info, dict) else str(info)
        price = 0.0
        if isinstance(info, dict):
            try:
                price = float(info.get('price', 0.0)) * CURRENCY_MULTIPLIER
            except Exception:
                price = 0.0
        else:
            # If the json value was just a number or string, try parse
            try:
                price = float(info) * CURRENCY_MULTIPLIER
            except Exception:
                price = 0.0

        product_database[bc] = {'name': name, 'price': round(price, 2)}

    # Debug output
    print("Loaded products from products.json:")
    for bc, info in product_database.items():
        print(f"  {bc} -> {info['name']}, {CURRENCY_SYMBOL}{info['price']:.2f}")

# initial load
load_products_from_json()

# --- SQLite DB init ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS products
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL,
                  price REAL NOT NULL,
                  barcode TEXT,
                  quantity INTEGER DEFAULT 1,
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

init_db()

# --- shared state ---
scanned_products = []
total_amount = 0.0
last_scan = {"barcode": "", "time": 0}
font = cv2.FONT_HERSHEY_PLAIN

# --- scanner thread ---
def qr_scanner():
    global scanned_products, total_amount, last_scan

    cv2.namedWindow("Smart Trolley - QR Scanner", cv2.WINDOW_AUTOSIZE)

    cap = None
    if USE_WEBCAM:
        cap = cv2.VideoCapture(WEBCAM_INDEX)
    else:
        url = IP_CAMERA_URL
        frame_suffix = CAMERA_FRAME_SUFFIX

    while True:
        try:
            if USE_WEBCAM:
                ret, frame = cap.read()
                if not ret:
                    print("Failed to read from webcam.")
                    time.sleep(1)
                    continue
            else:
                img_resp = urllib.request.urlopen(url + frame_suffix, timeout=5)
                imgnp = np.array(bytearray(img_resp.read()), dtype=np.uint8)
                frame = cv2.imdecode(imgnp, -1)

            decoded_objects = pyzbar.decode(frame)

            for obj in decoded_objects:
                barcode_data = obj.data.decode('utf-8').strip()
                if not barcode_data:
                    continue
                # normalize to uppercase to match product_database keys
                barcode_lookup = barcode_data.upper()
                current_time = time.time()

                if last_scan["barcode"] != barcode_lookup or (current_time - last_scan["time"]) > RESCAN_DELAY:
                    print("Type:", obj.type)
                    print("Data:", barcode_data)
                    last_scan["barcode"] = barcode_lookup
                    last_scan["time"] = current_time
                    process_barcode(barcode_lookup)

                cv2.putText(frame, str(barcode_data), (50, 50), font, 2,
                           (255, 0, 0), 3)
                cv2.putText(frame, f"Product Added! ({CURRENCY_SYMBOL})", (50, 100), font, 2,
                           (0, 255, 0), 3)

            cv2.imshow("Smart Trolley - QR Scanner", frame)
            key = cv2.waitKey(1)
            if key == 27:  # ESC
                break

        except Exception as e:
            print(f"Error in camera feed: {e}")
            time.sleep(1)

    cv2.destroyAllWindows()
    if cap:
        cap.release()

# --- barcode processing ---
def process_barcode(barcode_data_upper):
    global scanned_products, total_amount

    if barcode_data_upper in product_database:
        product = product_database[barcode_data_upper]

        # find existing
        found = False
        for item in scanned_products:
            if item['barcode'] == barcode_data_upper:
                item['quantity'] += 1
                item['total'] = round(item['quantity'] * item['price'], 2)
                found = True
                break

        if not found:
            new_product = {
                'name': product['name'],
                'price': product['price'],
                'barcode': barcode_data_upper,
                'quantity': 1,
                'total': round(product['price'], 2)
            }
            scanned_products.append(new_product)

        total_amount = round(sum(item['total'] for item in scanned_products), 2)
        save_to_database(barcode_data_upper, product['name'], product['price'])
        print(f"Added: {product['name']} - {CURRENCY_SYMBOL}{product['price']:.2f}")
    else:
        print(f"Product not found for barcode: {barcode_data_upper}")

# --- DB write ---
def save_to_database(barcode, name, price):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT * FROM products WHERE barcode=? 
                 AND DATE(timestamp)=DATE('now')''', (barcode,))
    existing = c.fetchone()
    if existing:
        c.execute('''UPDATE products SET quantity=quantity+1 
                     WHERE barcode=? AND DATE(timestamp)=DATE('now')''', (barcode,))
    else:
        c.execute('''INSERT INTO products (name, price, barcode, quantity)
                     VALUES (?, ?, ?, 1)''', (name, price, barcode))
    conn.commit()
    conn.close()

# --- Flask endpoints ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/cart')
def get_cart():
    return jsonify({
        'products': scanned_products,
        'total_amount': total_amount,
        'item_count': len(scanned_products),
        'currency_symbol': CURRENCY_SYMBOL
    })

@app.route('/api/clear')
def clear_cart():
    global scanned_products, total_amount
    scanned_products = []
    total_amount = 0.0
    return jsonify({'status': 'success'})

@app.route('/api/remove/<barcode>')
def remove_item(barcode):
    global scanned_products, total_amount
    bc = barcode.strip().upper()
    scanned_products[:] = [item for item in scanned_products if item['barcode'] != bc]
    total_amount = round(sum(item['total'] for item in scanned_products), 2)
    return jsonify({'status': 'success'})

@app.route('/api/increase/<barcode>')
def increase_quantity(barcode):
    global scanned_products, total_amount
    bc = barcode.strip().upper()
    for item in scanned_products:
        if item['barcode'] == bc:
            item['quantity'] += 1
            item['total'] = round(item['quantity'] * item['price'], 2)
            break
    total_amount = round(sum(item['total'] for item in scanned_products), 2)
    return jsonify({'status': 'success'})

@app.route('/api/decrease/<barcode>')
def decrease_quantity(barcode):
    global scanned_products, total_amount
    bc = barcode.strip().upper()
    for item in scanned_products:
        if item['barcode'] == bc:
            if item['quantity'] > 1:
                item['quantity'] -= 1
                item['total'] = round(item['quantity'] * item['price'], 2)
            else:
                scanned_products.remove(item)
            break
    total_amount = round(sum(item['total'] for item in scanned_products), 2)
    return jsonify({'status': 'success'})

# --- admin endpoint to reload products.json at runtime ---
@app.route('/api/reload-products', methods=['POST'])
def reload_products():
    load_products_from_json()
    return jsonify({'status': 'reloaded', 'product_count': len(product_database)})

# --- run ---
if __name__ == '__main__':
    scanner_thread = threading.Thread(target=qr_scanner, daemon=True)
    scanner_thread.start()
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG)
