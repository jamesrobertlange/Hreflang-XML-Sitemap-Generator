from flask import Flask, render_template, request, send_file, redirect, url_for, jsonify, flash
from werkzeug.utils import secure_filename
import os
from datetime import datetime
import csv
from collections import defaultdict
import re
import gzip
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom
import io
import zipfile
import time

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Ensure necessary folders exist
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'xml_sitemaps'
RAW_OUTPUT_FOLDER = 'raw_xml_sitemaps'
CSV_FOLDER = 'csv_output'
for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER, RAW_OUTPUT_FOLDER, CSV_FOLDER]:
    if not os.path.exists(folder):
        os.makedirs(folder)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Global variable to store progress
progress = {"status": "Not started", "percentage": 0}

def clear_output_folders():
    """Clear previous output files before new generation"""
    for folder in [OUTPUT_FOLDER, RAW_OUTPUT_FOLDER]:
        for file in os.listdir(folder):
            file_path = os.path.join(folder, file)
            try:
                if os.path.isfile(file_path):
                    os.unlink(file_path)
            except Exception as e:
                print(f"Error deleting {file_path}: {e}")

def parse_internal_csv(file_path, domain, csv_type='all'):
    """
    Parse the internal pages CSV file with case-insensitive domain matching.
    """
    pages = defaultdict(list)
    domain = domain.lower()  # Normalize domain for comparison
    
    try:
        with open(file_path, 'r', encoding='utf-8-sig') as csvfile:
            # Read the first line to check for separator specification
            first_line = csvfile.readline().strip()
            csvfile.seek(0)
            
            # Skip separator line if present
            if first_line.startswith('sep='):
                next(csvfile)
                first_line = csvfile.readline().strip()
                csvfile.seek(0)
                next(csvfile)
            
            # Check if the first line might be headers
            has_headers = 'url' in first_line.lower() or 'address' in first_line.lower() or 'full' in first_line.lower()
            
            if has_headers:
                reader = csv.DictReader(csvfile)
                fieldnames = reader.fieldnames
                normalized_fieldnames = {col: col.lower().replace(' ', '') for col in fieldnames}
                
                url_columns = ['fullurl', 'address', 'url']
                indexable_columns = ['isindexable', 'indexability', 'indexable']
                
                address_column = next((col for col, norm in normalized_fieldnames.items() 
                                    if norm in url_columns), None)
                indexability_column = next((col for col, norm in normalized_fieldnames.items() 
                                         if norm in indexable_columns), None)
                
                if not address_column or not indexability_column:
                    csvfile.seek(0)
                    if first_line.startswith('sep='):
                        next(csvfile)
                    reader = csv.reader(csvfile)
                    has_headers = False
            else:
                reader = csv.reader(csvfile)
            
            for row in reader:
                try:
                    if has_headers:
                        url = row[address_column].strip()
                        indexability = str(row[indexability_column]).strip().lower()
                    else:
                        if len(row) < 2:
                            continue
                        url = row[0].strip()
                        indexability = str(row[1]).strip().lower()
                    
                    if indexability not in ['true', 'indexable', 'yes', 'y', '1']:
                        continue
                    
                    if not url:
                        continue
                    
                    # Case-insensitive domain check
                    if domain not in url.lower():
                        if url.startswith('/'):
                            url = f"https://{domain}{url}"
                        else:
                            url = f"https://{domain}/{url}"
                    
                    # Extract path with case-insensitive matching
                    domain_pattern = re.compile(f"https?://(?:www\.)?{re.escape(domain)}(/.*)?", re.IGNORECASE)
                    path_match = domain_pattern.search(url)
                    
                    if path_match:
                        path = path_match.group(1) or '/'
                        base_url = url.split(path)[0].rstrip('/')
                        pages[base_url.lower()].append((url, path))
                    
                except Exception as e:
                    print(f"Error processing row: {row}")
                    print(f"Error details: {str(e)}")
                    continue
                    
            return pages
            
    except Exception as e:
        raise ValueError(f"Error parsing internal CSV: {str(e)}")

def parse_homepage_csv(file_path):
    """Parse the homepage CSV file."""
    homepages = defaultdict(dict)
    try:
        with open(file_path, 'r', encoding='utf-8-sig') as csvfile:
            # Read the first line to check for separator specification
            first_line = csvfile.readline().strip()
            
            # If it's a separator line, reset file pointer and skip first line
            if first_line.startswith('sep='):
                csvfile.seek(0)
                next(csvfile)  # Skip the separator line
            else:
                # If it's not a separator line, reset to start
                csvfile.seek(0)
            
            reader = csv.DictReader(csvfile)
            fieldnames = reader.fieldnames
            
            # Check for required columns with flexible naming
            homepage_col = next((col for col in fieldnames if col.lower() in ['homepage', 'url', 'address']), None)
            country_col = next((col for col in fieldnames if col.lower() in ['country', 'country code']), None)
            language_col = next((col for col in fieldnames if col.lower() in ['language', 'language code']), None)
            locale_col = next((col for col in fieldnames if col.lower() in ['locale', 'language tag']), None)
            default_col = next((col for col in fieldnames if col.lower() in ['language default', 'is default', 'default']), None)
            
            if not all([homepage_col, country_col, language_col]):
                available_columns = ", ".join(fieldnames)
                raise ValueError(f"Required columns missing. Need Homepage/URL, Country, and Language columns. Found columns: {available_columns}")
            
            for row in reader:
                url = row[homepage_col].rstrip('/')
                country = row[country_col].lower()
                language = row[language_col].lower()
                # Handle optional locale column
                locale = row[locale_col].lower() if locale_col else f"{language}_{country}"
                # Handle optional default column
                is_default = row.get(default_col, '').upper() == 'Y' if default_col else False
                
                key = f"{language}-{country}" if not is_default else language
                homepages[key] = {
                    'url': url,
                    'is_default': is_default,
                    'country': country,
                    'language': language,
                    'locale': locale
                }
                
        return homepages
        
    except Exception as e:
        raise ValueError(f"Error parsing homepage CSV: {str(e)}")

def get_uploaded_files():
    homepage_files = [f for f in os.listdir(UPLOAD_FOLDER) if f.endswith('_homepage.csv')]
    internal_files = [f for f in os.listdir(UPLOAD_FOLDER) if f.endswith('_internal.csv')]
    return homepage_files, internal_files

def normalize_url(url):
    """Normalize URL for consistent comparison"""
    url = url.lower().rstrip('/')
    # Remove query parameters but keep the main query indicator
    if '?' in url:
        base, query = url.split('?', 1)
        url = f"{base}?{query.split('&')[0]}"
    return url

def belongs_to_region(url, base_url, country, lang):
    """
    Check if a URL belongs to a specific region.
    For US/EN, the URL should NOT have any country/language codes.
    For other regions, the URL should have the specific country/language pattern.
    """
    norm_url = normalize_url(url)
    norm_base = normalize_url(base_url)
    
    if country.lower() == 'us' and lang.lower() == 'en':
        # For US/EN, ensure there's no country/language pattern
        pattern = re.compile(rf"{re.escape(norm_base)}/[a-z]{{2}}/[a-z]{{2}}/", re.IGNORECASE)
        return norm_url.startswith(norm_base) and not pattern.match(norm_url)
    else:
        # For other regions, ensure the correct country/language pattern exists
        pattern = rf"{re.escape(norm_base)}/{country}/{lang}/"
        return norm_url.startswith(normalize_url(pattern))

def get_url_sitemap_mapping(homepages, pages):
    """
    Maps URLs to their appropriate sitemaps based on homepage configurations.
    Returns both the mapped pages and a path mapping for alternates.
    """
    mapped_pages = defaultdict(list)
    path_mapping = defaultdict(set)
    
    # Find default homepage for x-default
    default_homepage = next((h for h in homepages.values() if h['is_default']), None)
    if not default_homepage:
        # If no default is marked, use US/EN as default
        default_homepage = next((h for h in homepages.values() 
                               if h['country'].lower() == 'us' and h['language'].lower() == 'en'), None)
    
    # First, extract and normalize all paths
    normalized_paths = {}
    url_variants = defaultdict(set)
    
    # Create pattern for extracting paths
    domain_pattern = re.compile(r'https?://[^/]+/(?:(?:[a-z]{2}/[a-z]{2}/)|(?:[a-z]{2}-[a-z]{2}/)|)?(.+)?', re.IGNORECASE)
    
    # First pass: collect all URLs and their normalized versions
    for url, path in pages:
        norm_url = normalize_url(url)
        match = domain_pattern.match(norm_url)
        if match:
            clean_path = match.group(1) or ''
            if clean_path:
                normalized_paths[norm_url] = clean_path
                path_mapping[clean_path].add(url)  # Keep original URL for output
                url_variants[clean_path].add(norm_url)

    # Now map URLs to appropriate sitemaps
    for lang_region, homepage in homepages.items():
        base_url = homepage['url'].rstrip('/')
        country = homepage['country'].lower()
        lang = homepage['language'].lower()
        
        # Add homepage to mapped pages
        mapped_pages[base_url].append((homepage['url'].rstrip('/') + '/', '/', True))  # True indicates homepage
        
        # For each normalized path, check if corresponding regional version exists
        for clean_path, variants in url_variants.items():
            matching_urls = [url for url in path_mapping[clean_path] 
                           if belongs_to_region(url, base_url, country, lang)]
            
            # If we find matching URLs, add them to the mapped pages
            for matching_url in matching_urls:
                mapped_pages[base_url].append((matching_url, f"/{clean_path}", False))

    return mapped_pages, path_mapping, default_homepage

def generate_sitemap(homepage, pages, all_homepages, path_mapping, default_homepage):
    """Generate a sitemap for a specific homepage and its pages with proper alternates and x-default."""
    urlset = Element('urlset', {
        'xmlns': 'http://www.sitemaps.org/schemas/sitemap/0.9',
        'xmlns:xhtml': 'http://www.w3.org/1999/xhtml'
    })

    links = []
    added_urls = set()

    # Add homepage with alternates
    homepage_elem = SubElement(urlset, 'url')
    homepage_loc = SubElement(homepage_elem, 'loc')
    homepage_loc.text = homepage['url'].rstrip('/') + '/'
    
    # Add x-default for homepage
    if default_homepage:
        x_default = SubElement(homepage_elem, 'xhtml:link', {
            'rel': 'alternate',
            'hreflang': 'x-default',
            'href': default_homepage['url'].rstrip('/') + '/'
        })
    
    # Add homepage alternates
    for lang_region, alt_home in all_homepages.items():
        hreflang = lang_region if alt_home['is_default'] else f"{alt_home['language']}-{alt_home['country']}".lower()
        alt_url = alt_home['url'].rstrip('/') + '/'
        
        link = SubElement(homepage_elem, 'xhtml:link', {
            'rel': 'alternate',
            'hreflang': hreflang,
            'href': alt_url
        })
        
        sitemap_id = f"{homepage['country']}_{homepage['language']}_{homepage['locale']}"
        links.append((alt_url, sitemap_id, hreflang))
    
    added_urls.add(normalize_url(homepage['url']) + '/')

    # Add internal pages with alternates
    for full_url, path, is_homepage in pages:
        if is_homepage or normalize_url(full_url) in added_urls:
            continue
            
        # Extract the normalized path
        match = re.match(r'https?://[^/]+/(?:(?:[a-z]{2}/[a-z]{2}/)|(?:[a-z]{2}-[a-z]{2}/)|)?(.+)?', 
                        full_url, 
                        re.IGNORECASE)
        if not match:
            continue
            
        clean_path = match.group(1)
        if not clean_path:
            continue

        # Create URL element
        url_elem = SubElement(urlset, 'url')
        loc = SubElement(url_elem, 'loc')
        loc.text = full_url
        added_urls.add(normalize_url(full_url))

        # Add x-default for this URL
        if default_homepage:
            # Construct x-default URL based on default homepage
            if default_homepage['country'].lower() == 'us' and default_homepage['language'].lower() == 'en':
                x_default_url = f"{default_homepage['url'].rstrip('/')}/{clean_path}"
            else:
                x_default_url = f"{default_homepage['url'].rstrip('/')}/{default_homepage['country']}/{default_homepage['language']}/{clean_path}"
            
            if normalize_url(x_default_url) in {normalize_url(u) for u in path_mapping.get(clean_path, set())}:
                SubElement(url_elem, 'xhtml:link', {
                    'rel': 'alternate',
                    'hreflang': 'x-default',
                    'href': x_default_url
                })

        # Add alternates for all matching paths
        for lang_region, alt_home in all_homepages.items():
            hreflang = lang_region if alt_home['is_default'] else f"{alt_home['language']}-{alt_home['country']}".lower()
            
            # Construct alternate URL
            if alt_home['country'].lower() == 'us' and alt_home['language'].lower() == 'en':
                alt_url = f"{alt_home['url'].rstrip('/')}/{clean_path}"
            else:
                alt_url = f"{alt_home['url'].rstrip('/')}/{alt_home['country']}/{alt_home['language']}/{clean_path}"
            
            # Check if the alternate URL exists (case-insensitive)
            normalized_alt = normalize_url(alt_url)
            matching_urls = [u for u in path_mapping.get(clean_path, set()) 
                           if normalize_url(u) == normalized_alt]
            
            if matching_urls:
                actual_url = matching_urls[0]  # Use the original URL with proper case
                link = SubElement(url_elem, 'xhtml:link', {
                    'rel': 'alternate',
                    'hreflang': hreflang,
                    'href': actual_url
                })
                
                sitemap_id = f"{homepage['country']}_{homepage['language']}_{homepage['locale']}"
                links.append((actual_url, sitemap_id, hreflang))

    return urlset, links

def generate_sitemaps(homepages, pages):
    """
    Generate all sitemaps with proper URL mapping.
    """
    # Map URLs to appropriate sitemaps and get path mapping
    mapped_pages, path_mapping, default_homepage = get_url_sitemap_mapping(homepages, [
        (url, path) for base_url, url_list in pages.items() 
        for url, path in url_list
    ])
    
    sitemap_files = []
    all_links = []
    today = datetime.now().strftime("%Y%m%d")

    for lang_region, homepage in homepages.items():
        base_url = homepage['url'].rstrip('/')
        
        # Get pages for this sitemap
        sitemap_pages = mapped_pages.get(base_url, [])
        
        # Generate sitemap with default_homepage parameter
        urlset, links = generate_sitemap(homepage, sitemap_pages, homepages, path_mapping, default_homepage)
        all_links.extend(links)
        
        # Save sitemap
        filename = f"sitemap_{today}_{homepage['country']}_{homepage['language']}_{homepage['locale']}.xml.gz"
        raw_filename = f"sitemap_{today}_{homepage['country']}_{homepage['language']}_{homepage['locale']}.xml"
        
        sitemap_path = os.path.join(OUTPUT_FOLDER, filename)
        raw_sitemap_path = os.path.join(RAW_OUTPUT_FOLDER, raw_filename)
        
        save_sitemap(urlset, sitemap_path, raw_sitemap_path)
        sitemap_files.append((filename, raw_filename))
    
    return sitemap_files, all_links

def save_sitemap(urlset, filename, raw_filename):
    """Save sitemap in both raw and compressed formats."""
    rough_string = tostring(urlset, 'utf-8')
    reparsed = minidom.parseString(rough_string)
    pretty_xml = reparsed.toprettyxml(indent="  ")
    
    # Save uncompressed XML
    with open(raw_filename, 'w', encoding='utf-8') as f:
        f.write(pretty_xml)
    
    # Save gzipped XML
    with gzip.open(filename, 'wt', encoding='utf-8') as f:
        f.write(pretty_xml)

def create_zip_file(source_folder, zip_filename):
    """Create a zip file from a folder."""
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

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        try:
            # Reset progress and clear previous outputs
            global progress
            progress = {"status": "Starting", "percentage": 0}
            clear_output_folders()

            domain = request.form.get('domain', '').strip()
            csv_type = request.form.get('csv_type', 'standard')
            
            if not domain:
                raise ValueError('Please enter a domain name')

            # Handle file uploads/selection
            homepage_file = request.files.get('homepage_file')
            internal_file = request.files.get('internal_file')
            homepage_select = request.form.get('homepage_select')
            internal_select = request.form.get('internal_select')

            # Process homepage file
            if homepage_file and homepage_file.filename:
                homepage_filename = f"{datetime.now().strftime('%Y%m%d')}_{secure_filename(homepage_file.filename.rsplit('.', 1)[0])}_homepage.csv"
                homepage_path = os.path.join(app.config['UPLOAD_FOLDER'], homepage_filename)
                homepage_file.save(homepage_path)
            elif homepage_select:
                homepage_path = os.path.join(app.config['UPLOAD_FOLDER'], homepage_select)
            else:
                raise ValueError('Please upload or select a homepage file')

            # Process internal file
            if internal_file and internal_file.filename:
                internal_filename = f"{datetime.now().strftime('%Y%m%d')}_{secure_filename(internal_file.filename.rsplit('.', 1)[0])}_internal.csv"
                internal_path = os.path.join(app.config['UPLOAD_FOLDER'], internal_filename)
                internal_file.save(internal_path)
            elif internal_select:
                internal_path = os.path.join(app.config['UPLOAD_FOLDER'], internal_select)
            else:
                raise ValueError('Please upload or select an internal pages file')

            # Process files and generate sitemaps
            progress["status"] = "Parsing homepage data..."
            progress["percentage"] = 10
            homepages = parse_homepage_csv(homepage_path)
            
            progress["status"] = "Parsing internal pages..."
            progress["percentage"] = 20
            internal_pages = parse_internal_csv(internal_path, domain, csv_type)
            
            progress["status"] = "Generating sitemaps..."
            progress["percentage"] = 40

            # Generate sitemaps with the new mapping logic
            sitemap_files, all_links = generate_sitemaps(homepages, internal_pages)

            # Save all links to CSV
            today = datetime.now().strftime("%Y%m%d")
            csv_filename = os.path.join(CSV_FOLDER, f"all_links_{today}.csv")
            with open(csv_filename, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(['URL', 'Sitemap', 'Hreflang'])
                writer.writerows(all_links)

            progress["status"] = "Complete"
            progress["percentage"] = 100

            flash('Sitemaps generated successfully!')
            return jsonify({"status": "success", "redirect": url_for('success')})

        except Exception as e:
            progress["status"] = "Error"
            progress["percentage"] = 0
            flash(str(e))
            return jsonify({"status": "error", "message": str(e)}), 400

    # GET request
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
    try:
        if not os.path.exists(OUTPUT_FOLDER) or not os.listdir(OUTPUT_FOLDER):
            flash('No compressed sitemaps found. Please generate the sitemaps first.')
            return redirect(url_for('index'))
        return create_zip_file(OUTPUT_FOLDER, 'compressed_sitemaps.zip')
    except Exception as e:
        flash(f'Error downloading compressed sitemaps: {str(e)}')
        return redirect(url_for('index'))

@app.route('/download_raw')
def download_raw():
    try:
        if not os.path.exists(RAW_OUTPUT_FOLDER) or not os.listdir(RAW_OUTPUT_FOLDER):
            flash('No raw sitemaps found. Please generate the sitemaps first.')
            return redirect(url_for('index'))
        return create_zip_file(RAW_OUTPUT_FOLDER, 'raw_sitemaps.zip')
    except Exception as e:
        flash(f'Error downloading raw sitemaps: {str(e)}')
        return redirect(url_for('index'))

@app.route('/download_csv')
def download_csv():
    try:
        today = datetime.now().strftime("%Y%m%d")
        csv_filename = f"all_links_{today}.csv"
        csv_path = os.path.join(CSV_FOLDER, csv_filename)
        
        if not os.path.exists(csv_path):
            flash('No CSV file found. Please generate the sitemaps first.')
            return redirect(url_for('index'))
        
        return send_file(
            csv_path,
            download_name=csv_filename,
            as_attachment=True,
            mimetype='text/csv'
        )
    except Exception as e:
        flash(f'Error downloading CSV: {str(e)}')
        return redirect(url_for('index'))

if __name__ == '__main__':
    # Ensure the upload and output directories exist
    for directory in [UPLOAD_FOLDER, OUTPUT_FOLDER, RAW_OUTPUT_FOLDER]:
        if not os.path.exists(directory):
            os.makedirs(directory)
    
    # Run the Flask application
    app.run(debug=True, host='0.0.0.0', port=5000)