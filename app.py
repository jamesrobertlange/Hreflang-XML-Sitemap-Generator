import os
from flask import Flask, render_template, request, send_file, redirect, url_for, jsonify
from werkzeug.utils import secure_filename
import csv
import sys
from collections import defaultdict, Counter
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom
import re
import gzip
import zipfile
import io
from tqdm import tqdm

app = Flask(__name__)

# Ensure necessary folders exist
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'xml_sitemaps'
RAW_OUTPUT_FOLDER = 'raw_xml_sitemaps'
for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER, RAW_OUTPUT_FOLDER]:
    if not os.path.exists(folder):
        os.makedirs(folder)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Global variable to store progress
progress = {"status": "", "percentage": 0}

def parse_homepage_csv(file_path):
    homepages = defaultdict(dict)
    with open(file_path, 'r', encoding='utf-8-sig') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            url = row['Homepage'].rstrip('/')
            country = row['Country'].lower()
            language = row['Language'].lower()
            locale = row['Locale'].lower()
            is_default = row['Language Default'] == 'Y'
            
            key = f"{language}-{country}" if not is_default else language
            homepages[key] = {
                'url': url,
                'is_default': is_default,
                'country': country,
                'language': language,
                'locale': locale
            }
    return homepages

def parse_internal_csv(file_path):
    pages = defaultdict(list)
    with open(file_path, 'r', encoding='utf-8-sig') as csvfile:
        reader = csv.DictReader(csvfile)
        fieldnames = reader.fieldnames
        address_column = next((col for col in fieldnames if col.lower() == 'address'), None)
        indexability_column = next((col for col in fieldnames if col.lower() == 'indexability'), None)
        
        if not address_column or not indexability_column:
            raise ValueError("Required columns not found in the CSV file.")
        
        for row in reader:
            url = row[address_column]
            indexability = row[indexability_column].lower()
            
            if indexability != 'indexable':
                continue
            
            match = re.match(r'(https?://(?:www\.)?mauijim\.com)(?:/([^/]+)/([^/]+))?(/.*)$', url, re.IGNORECASE)
            if match:
                base_url, country, lang, path = match.groups()
                base_url = base_url.rstrip('/')
                path = path or '/'
                
                if country and lang:
                    key = f"{base_url}/{country}/{lang}".lower()
                else:
                    key = base_url.lower()
                
                pages[key].append((url, path))
    return pages

def generate_sitemap(homepage, pages, all_homepages):
    urlset = Element('urlset', {
        'xmlns': 'http://www.sitemaps.org/schemas/sitemap/0.9',
        'xmlns:xhtml': 'http://www.w3.org/1999/xhtml'
    })

    links = []  # To store all links for CSV generation

    # Add homepage
    url_elem = SubElement(urlset, 'url')
    loc = SubElement(url_elem, 'loc')
    loc.text = homepage['url'] + '/'
    
    links.append((homepage['url'] + '/', f"{homepage['country']}_{homepage['language']}_{homepage['locale']}", "x-default"))

    for lang_region, home in all_homepages.items():
        if home['is_default']:
            hreflang = lang_region
        else:
            hreflang = f"{home['language']}-{home['country']}"
        
        link = SubElement(url_elem, 'xhtml:link', {
            'rel': 'alternate',
            'hreflang': hreflang,
            'href': home['url'] + '/'
        })
        links.append((home['url'] + '/', f"{homepage['country']}_{homepage['language']}_{homepage['locale']}", hreflang))

    # Add internal pages
    base_url = homepage['url'].rstrip('/')
    added_urls = set([homepage['url'] + '/'])  # Track added URLs to avoid duplicates

    for full_url, path in pages:
        if full_url not in added_urls:
            url_elem = SubElement(urlset, 'url')
            loc = SubElement(url_elem, 'loc')
            loc.text = full_url
            added_urls.add(full_url)

            for lang_region, home in all_homepages.items():
                if home['is_default']:
                    hreflang = lang_region
                else:
                    hreflang = f"{home['language']}-{home['country']}"
                
                alt_url = f"{home['url']}{path}" if path != '/' else home['url'] + '/'
                link = SubElement(url_elem, 'xhtml:link', {
                    'rel': 'alternate',
                    'hreflang': hreflang,
                    'href': alt_url
                })
                links.append((alt_url, f"{homepage['country']}_{homepage['language']}_{homepage['locale']}", hreflang))

    return urlset, links

def save_sitemap(urlset, filename, raw_filename):
    rough_string = tostring(urlset, 'utf-8')
    reparsed = minidom.parseString(rough_string)
    pretty_xml = reparsed.toprettyxml(indent="  ")
    
    # Save uncompressed XML
    with open(raw_filename, 'w', encoding='utf-8') as f:
        f.write(pretty_xml)
    
    # Save gzipped XML
    with gzip.open(filename, 'wt', encoding='utf-8') as f:
        f.write(pretty_xml)

def get_uploaded_files():
    homepage_files = [f for f in os.listdir(UPLOAD_FOLDER) if f.endswith('_homepage.csv')]
    internal_files = [f for f in os.listdir(UPLOAD_FOLDER) if f.endswith('_internal.csv')]
    return homepage_files, internal_files

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        # Handle file selection or upload
        homepage_file = request.files.get('homepage_file')
        internal_file = request.files.get('internal_file')
        homepage_select = request.form.get('homepage_select')
        internal_select = request.form.get('internal_select')

        if homepage_file and homepage_file.filename != '':
            homepage_filename = secure_filename(homepage_file.filename)
            homepage_filename = f"{datetime.now().strftime('%Y%m%d')}_{homepage_filename.rsplit('.', 1)[0]}_homepage.csv"
            homepage_path = os.path.join(app.config['UPLOAD_FOLDER'], homepage_filename)
            homepage_file.save(homepage_path)
        elif homepage_select:
            homepage_path = os.path.join(app.config['UPLOAD_FOLDER'], homepage_select)
        else:
            return redirect(request.url)

        if internal_file and internal_file.filename != '':
            internal_filename = secure_filename(internal_file.filename)
            internal_filename = f"{datetime.now().strftime('%Y%m%d')}_{internal_filename.rsplit('.', 1)[0]}_internal.csv"
            internal_path = os.path.join(app.config['UPLOAD_FOLDER'], internal_filename)
            internal_file.save(internal_path)
        elif internal_select:
            internal_path = os.path.join(app.config['UPLOAD_FOLDER'], internal_select)
        else:
            return redirect(request.url)

        # Reset progress
        global progress
        progress = {"status": "Starting", "percentage": 0}

        # Process the files
        homepages = parse_homepage_csv(homepage_path)
        internal_pages = parse_internal_csv(internal_path)

        today = datetime.now().strftime("%Y%m%d")
        all_links = []

        total_homepages = len(homepages)
        for i, (lang_region, homepage) in enumerate(homepages.items()):
            progress["status"] = f"Processing {lang_region}"
            progress["percentage"] = int((i / total_homepages) * 100)

            base_url = homepage['url'].rstrip('/')
            
            if homepage['is_default']:
                default_pages = [(url, path) for url, path in internal_pages.get(base_url.lower(), [])
                                 if url == base_url + '/' or '/US/en_US/' in url]
                us_pages = internal_pages.get(f"{base_url.lower()}/us/en_us", [])
                pages = default_pages + us_pages
            else:
                pages = internal_pages.get(base_url.lower(), [])
            
            urlset, links = generate_sitemap(homepage, pages, homepages)
            all_links.extend(links)
            
            filename = os.path.join(OUTPUT_FOLDER, f"sitemap_{today}_{homepage['country']}_{homepage['language']}_{homepage['locale']}.xml.gz")
            raw_filename = os.path.join(RAW_OUTPUT_FOLDER, f"sitemap_{today}_{homepage['country']}_{homepage['language']}_{homepage['locale']}.xml")
            save_sitemap(urlset, filename, raw_filename)

        # Generate CSV with all links
        csv_filename = f"all_links_{today}.csv"
        with open(csv_filename, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['URL', 'Sitemap', 'Hreflang'])
            writer.writerows(all_links)

        progress["status"] = "Complete"
        progress["percentage"] = 100

        return redirect(url_for('success'))

    homepage_files, internal_files = get_uploaded_files()
    return render_template('index.html', homepage_files=homepage_files, internal_files=internal_files)

@app.route('/progress')
def get_progress():
    return jsonify(progress)

@app.route('/success')
def success():
    return render_template('success.html')

@app.route('/download_compressed')
def download_compressed():
    return create_zip_file(OUTPUT_FOLDER, 'compressed_sitemaps.zip')

@app.route('/download_raw')
def download_raw():
    return create_zip_file(RAW_OUTPUT_FOLDER, 'raw_sitemaps.zip')

@app.route('/download_csv')
def download_csv():
    today = datetime.now().strftime("%Y%m%d")
    csv_filename = f"all_links_{today}.csv"
    return send_file(
        csv_filename,
        download_name=csv_filename,
        as_attachment=True,
        mimetype='text/csv'
    )

def create_zip_file(source_folder, zip_filename):
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(source_folder):
            for file in files:
                zf.write(os.path.join(root, file), file)
    memory_file.seek(0)
    return send_file(
        memory_file,
        download_name=zip_filename,
        as_attachment=True,
        mimetype='application/zip'
    )

if __name__ == '__main__':
    app.run(debug=True)