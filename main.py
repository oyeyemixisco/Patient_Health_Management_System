# Importing modules
import csv
import io, os, uuid
from datetime import datetime
from flask import Flask, request, render_template, redirect, session, url_for, flash, jsonify, send_file, abort
import mysql.connector
from constants import ECG_COLUMNS, DATASET_REGISTRY, DATE_COLUMNS
from werkzeug.utils import secure_filename
import tempfile
import cv2
import base64
from pathlib import Path
import numpy as np
import wfdb
import pandas as pd
from io import BytesIO
from PIL import Image
from scipy import ndimage
from scipy.signal import firwin, lfilter
from skimage.segmentation import clear_border
from skimage.measure import label, regionprops



# declaring Flask app
app = Flask(__name__)
app.secret_key = "NMWW_uy98898_iuUYTTHGY_98HHH"

BASE_DIR = Path(__file__).resolve().parent
LOCAL_RECORD_BASE = BASE_DIR / "static" / "data" / "00001_lr"


# connect to the MySQL using the mysql.connector
def get_db_connection():
    conn = mysql.connector.connect(
        host="localhost",
        user="root",
        password="",
        database="health"
    )
    return conn

def to_number(value, default=None):
    if value in ("", None):
        return default
    try:
        return float(value)
    except ValueError:
        return default

# Route for the home page (to shows the form 'index.html')
@app.route('/', methods=['GET', 'POST'])
def index():
    #If lready logged in, go to home
    if 'admin_id' in session:
        return redirect(url_for('home'))

    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT * FROM admin WHERE email=%s AND password=%s",
            (email, password)
        )
        admin = cursor.fetchone()

        cursor.close()
        conn.close()

        if admin:
            session['admin_id'] = admin['id']
            session['admin_email'] = admin['email']
            return redirect(url_for('home'))
        else:
            flash("Invalid email or password", "danger")

    return render_template('login.html')
    
# Route for the admin dashboard
@app.route('/home')
def home():
    if 'admin_id' not in session:
        return redirect(url_for('index'))

    return render_template('index.html')

# route for logout
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# route for help page
@app.route('/settings')
def settings():
    return render_template('help.html')

# Route for the edit-profile page
@app.route("/edit-profile")
def edit_profile():
    return render_template("edit-profile.html")

# Route for the register page  
@app.route('/register')
def register():
    return render_template('register.html')

# Route for add-patient page
@app.route("/add_patients", methods=["GET", "POST"])
def add_patients():
    if request.method == "POST":
        # Get form data
        first_name   = request.form.get("First_Name")
        last_name    = request.form.get("Last_Name")
        phone        = request.form.get("Phone")
        email        = request.form.get("Email")
        address      = request.form.get("Address")
        postal_code  = request.form.get("Postal_Code")
        country      = request.form.get("Country")
        gender       = request.form.get("Gender")
        dob          = request.form.get("DOB")     
        city         = request.form.get("City")
        department   = request.form.get("Department")

        # Save to DB
        conn = get_db_connection()
        cursor = conn.cursor()

        insert_sql = """
            INSERT INTO patients 
                (First_Name, Last_Name, Phone, Email, Address, Postal_Code, Country, Gender, DOB, City, Department)
            VALUES 
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        cursor.execute(insert_sql, (
            first_name,
            last_name,
            phone,
            email,
            address,
            postal_code,
            country,
            gender,
            dob,
            city,
            department
        ))

        conn.commit()
        cursor.close()
        conn.close()

        flash("Patient added successfully!", "success")  
        return redirect(url_for("patient_mgm"))  

        # After saving, go back to patient list
        return redirect(url_for("patients_list"))

    # If GET, just show the empty form
    return render_template("add-patients.html")

# Route for the patient-profile page
@app.route("/patient/<int:patient_id>")
def patient_detail(patient_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM patients WHERE id = %s", (patient_id,))
    patient = cursor.fetchone()

    cursor.close()
    conn.close()

    if not patient:
        return "Patient not found", 404

    return render_template("patients-profile.html", patient=patient)

# Route for the profile page
@app.route('/profile')
def profile():
    return render_template('profile.html')

@app.route('/doctor_dashboard')
def doctor_dashboard():
    return render_template('doctor-dashboard.html')

# Route for the patient management page
@app.route("/patient-mgm")
def patient_mgm():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Total patients
    cursor.execute("SELECT COUNT(*) AS total FROM patients")
    total_patients = cursor.fetchone()["total"]

    # Count by gender
    cursor.execute("""
        SELECT Gender, COUNT(*) AS count
        FROM patients
        GROUP BY Gender
    """)
    gender_rows = cursor.fetchall()

    male_patients = 0
    female_patients = 0

    for row in gender_rows:
        gender = (row["Gender"] or "").strip().lower()
        if gender.startswith("m"):
            male_patients = row["count"]
        elif gender.startswith("f"):
            female_patients = row["count"]

    cursor.close()
    conn.close()

    return render_template(
        "patient-mgm.html",
        total_patients=total_patients,
        male_patients=male_patients,
        female_patients=female_patients,
    )


# Route for the patient dashboard page
@app.route("/patient-dashboard")
def patient_dashboard():
    return render_template("patient-dashboard.html")


# route to get the patients data from the database and render it in the template
@app.route("/patients")
def patients_list():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)  

    cursor.execute("SELECT * FROM `patients` ")
    patients = cursor.fetchall()

    cursor.close()
    conn.close()

    # pass to template
    return render_template("patient-list.html", patients=patients)

def parse_date(val: str):
    if not val:
        return None
    val = val.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(val, fmt).date()
        except ValueError:
            pass
    return None

# Route for the load-data page
@app.route("/load-data", methods=["GET", "POST"])
def load_data():
    preview_rows = []
    temp_path = None 
    inserted = 0
    page = 1
    total_pages = 1
    upload_mode = None
    selected_table = request.args.get("table") 
    selected_dataset = selected_table 

    # Temporary uploads folder
    upload_folder = os.path.join(tempfile.gettempdir(), "uploads")
    os.makedirs(upload_folder, exist_ok=True)

    # CSV Upload (ECG) 
    if request.method == "POST":
        f = request.files.get("csv_file")
        if not f or f.filename == "":
            flash("Please choose a CSV file.", "danger")
            return redirect(url_for("load_data"))

        if not f.filename.lower().endswith(".csv"):
            flash("Only .csv files are allowed.", "danger")
            return redirect(url_for("load_data"))

        # Read CSV content
        content = f.read().decode("utf-8", errors="ignore")
        stream = io.StringIO(content)
        reader = csv.DictReader(stream)
        uploaded_cols = reader.fieldnames or []

        # Check if all required ECG columns are present
        missing = [c for c in ECG_COLUMNS if c not in uploaded_cols]

        # Read all rows
        stream.seek(0)
        reader = csv.DictReader(stream)
        all_rows = list(reader)

        is_ecg = not missing

        if is_ecg:
            preview_columns = DATASET_REGISTRY["patient_csv"]["columns"]
            preview_rows = [
                {col: row.get(col) for col in preview_columns}
                for row in all_rows[:10]
            ]
        else:
            preview_columns = uploaded_cols
            preview_rows = all_rows[:10]


        if missing:
            # CSV missing required columns then preview only
            upload_mode = "preview_only"
            session["uploaded_rows"] = preview_rows
            session["upload_mode"] = "preview_only"
            session["uploaded_columns"] = uploaded_cols
            session["uploaded_row_count"] = len(all_rows)
            flash(
                "⚠️ This dataset does not match the ECG structure. "
                "Preview only; cannot save to DB.",
                "warning"
            )
        else:
            # CSV valid then can save
            upload_mode = "ecg_valid"
            session["upload_mode"] = "ecg_valid"
            session["uploaded_row_count"] = len(all_rows)
            session["uploaded_file"] = temp_path

            # Save temporary file
            filename = secure_filename(f.filename)
            upload_folder = os.path.join(tempfile.gettempdir(), "uploads")
            os.makedirs(upload_folder, exist_ok=True)
            temp_path = os.path.join(upload_folder, filename)
            f.seek(0)
            f.save(temp_path)
            session["uploaded_file"] = temp_path

            flash("✔ ECG dataset validated. You may review and save it.", "success")

        return render_template(
            "load-data.html",
            preview_rows=preview_rows,
            preview_columns=preview_columns,
            uploaded_cols=uploaded_cols,
            inserted=0,
            upload_mode=upload_mode,
            page=1,
            total_pages=1,
            datasets=DATASET_REGISTRY,
            selected_dataset=None,
            dataset_info=None,
            dataset_metadata=None,
            date_columns=DATE_COLUMNS
        )


    # Database Preview
    elif request.method == "GET" and request.args.get("source") == "db":
        # Show dataset selector if no table selected
        if not selected_table:
            return render_template(
                "load-data.html",
                datasets=DATASET_REGISTRY,
                preview_rows=[],
                page=1,
                total_pages=1,
                selected_dataset=None,
                dataset_info=None,
                dataset_metadata=None,
                date_columns=DATE_COLUMNS
            )

        # Validate dataset choice
        if selected_table not in DATASET_REGISTRY:
            flash("Invalid dataset selected.", "danger")
            return redirect(url_for("load_data", source="db"))

        dataset = DATASET_REGISTRY[selected_table]
        columns = dataset["columns"]

        # Metadata
        dataset_metadata = {}
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"SELECT COUNT(*) AS total FROM `{selected_table}`")
        total_rows = cursor.fetchone()["total"]
        cursor.execute(f"SHOW COLUMNS FROM `{selected_table}`")
        col_names = [row["Field"] for row in cursor.fetchall()]
        dataset_metadata = {
            "name": selected_table,
            "source": "Database",
            "row_count": total_rows,
            "columns": col_names
        }

        # Pagination
        page = request.args.get("page", 1, type=int)
        per_page = 15
        offset = (page - 1) * per_page 

        column_sql = ", ".join(columns)
        query = f"SELECT {column_sql} FROM `{selected_table}` LIMIT %s OFFSET %s"
        cursor.execute(query, (per_page, offset))
        preview_rows = cursor.fetchall()

        total_pages = (total_rows + per_page - 1) // per_page
        cursor.close()
        conn.close()

        return render_template(
            "load-data.html",
            datasets=DATASET_REGISTRY,
            selected_dataset=selected_dataset,
            dataset_info=dataset,
            dataset_metadata=dataset_metadata,
            preview_rows=preview_rows,
            page=page,
            total_pages=total_pages,
            date_columns=DATE_COLUMNS
        )

    return render_template(
        "load-data.html",
        preview_rows=[],
        inserted=0,
        upload_mode=None,
        page=1,
        total_pages=1,
        datasets=DATASET_REGISTRY,
        selected_dataset=None,
        dataset_info=None,
        dataset_metadata=None,
        date_columns=DATE_COLUMNS
    )

from datetime import datetime

def parse_date2(value):
    if not value or value in ("", "NULL"):
        return None

    formats = [
        "%Y-%m-%d %H:%M:%S",  
        "%Y-%m-%d",          
        "%d/%m/%Y",
        "%d-%m-%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass

    return None



# Route to save validated ECG data to the database
@app.route("/save-ecg-data", methods=["POST"])
def save_ecg_data():
    BATCH_SIZE = 500

    # only allow valid uploads
    if session.get("upload_mode") != "ecg_valid":
        flash("This dataset cannot be saved.", "danger")
        return redirect(url_for("load_data"))

    file_path = session.get("uploaded_file")
    if not file_path or not os.path.exists(file_path):
        flash("Uploaded file not found.", "danger")
        return redirect(url_for("load_data"))

    # Read CSV
    with open(file_path, newline="", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        flash("No data to save.", "warning")
        return redirect(url_for("load_data"))

    # Prepare rows
    rows_to_insert = []
    for row in rows:
        rows_to_insert.append((
            row.get("ecg_id"),
            row.get("patient_id"),
            row.get("age"),
            row.get("sex"),
            row.get("height") or 0,
            row.get("weight") or 0,
            row.get("nurse"),
            row.get("site"),
            row.get("device"),
            parse_date2(row.get("recording_date")),
            row.get("report"),
            row.get("scp_codes"),
            row.get("heart_axis"),
            row.get("infarction_stadium1"),
            row.get("infarction_stadium2"),
            row.get("validated_by"),
            row.get("second_opinion"),
            row.get("initial_autogenerated_report"),
            row.get("validated_by_human"),
            row.get("baseline_drift") or 0,
            row.get("static_noise") or 0,
            row.get("burst_noise") or 0,
            row.get("electrodes_problems") or 0,
            row.get("extra_beats") or 0,
            row.get("pacemaker") or 0,
            row.get("strat_fold"),
            row.get("filename_lr"),
            row.get("filename_hr"),
        ))

    # Insert in batches
    conn = get_db_connection()
    cursor = conn.cursor()
    inserted = 0

    sql = """
        INSERT INTO patient_csv (
            ecg_id, patient_id, age, sex, height, weight,
            nurse, site, device, recording_date,
            report, scp_codes, heart_axis,
            infarction_stadium1, infarction_stadium2,
            validated_by, second_opinion,
            initial_autogenerated_report,
            validated_by_human,
            baseline_drift, static_noise, burst_noise,
            electrodes_problems, extra_beats, pacemaker,
            strat_fold, filename_lr, filename_hr
        )
        VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s,
            %s,
            %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s
        )
    """

    try:
        for i in range(0, len(rows_to_insert), BATCH_SIZE):
            batch = rows_to_insert[i:i + BATCH_SIZE]
            cursor.executemany(sql, batch)
            conn.commit()
            inserted += cursor.rowcount
    finally:
        cursor.close()
        conn.close()

    # prevent duplicate re-submission
    session.pop("uploaded_file", None)
    session.pop("upload_mode", None)

    flash(f"✔ Successfully inserted {inserted} ECG records.", "success")
    return redirect(url_for("load_data"))

# route to get row details
@app.route("/api/db/row-details")
def row_details():
    table = request.args.get("table")
    row_id = request.args.get("id")

    if not table or not row_id:
        return jsonify({"error": "Missing parameters"}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        f"SELECT * FROM `{table}` WHERE id = %s",
        (row_id,)
    )

    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        return jsonify({"error": "Not found"}), 404

    return jsonify(row)

# route to update a row
@app.route("/api/db/update-row", methods=["POST"])
def update_row():
    payload = request.get_json()
    table = payload.get("table")
    row_id = payload.get("id")
    data = payload.get("data")

    if not table or not row_id or not data:
        return jsonify({"error": "Missing parameters"}), 400

    # Convert date fields to proper date format
    for date_col in DATE_COLUMNS.get(table, []):
        if date_col in data and data[date_col]:
            data[date_col] = parse_date(data[date_col])

    conn = get_db_connection()
    cursor = conn.cursor()

    cols = ", ".join([f"`{k}`=%s" for k in data.keys()])
    vals = list(data.values())
    vals.append(row_id)
    sql = f"UPDATE `{table}` SET {cols} WHERE id=%s"

    try:
        cursor.execute(sql, vals)
        conn.commit()
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        cursor.close()
        conn.close()

    return jsonify({"success": True})


# route to delete a row
@app.route("/delete-row", methods=["POST"])
def delete_row():
    table = request.form.get("table")
    row_id = request.form.get("row_id")

    if not table or not row_id:
        flash("Missing data for deletion.", "danger")
        return redirect(url_for("load_data", source="db", table=table))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        sql = f"DELETE FROM `{table}` WHERE id = %s"
        cursor.execute(sql, (row_id,))
        conn.commit()
        flash("Row deleted successfully.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Error deleting row: {str(e)}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for("load_data", source="db", table=table))

# Route for image processing dashboard
@app.route("/image-processing")
def image_processing():
    return render_template("image-processing.html")

# Route for image processing tasks
@app.route("/img_processing")
def img_processing():
    return render_template("processing.html")

# Route for signal processing dashboard
@app.route("/signal")
def signal():
    return render_template("signal.html")

# route for signal processing tasks
@app.route("/signal_processing")
def signal_processing():
    return render_template("signal-processing.html")

# route for health analysis dashboard
@app.route("/health")
def health():
    return render_template("health.html")

# route for health analysis tasks
@app.route("/health_analysis", methods=["GET", "POST"])
def health_analysis():
    # If the user is submitting the 'Load Signal' form
    if request.method == "POST":
        source_type = request.form.get("sourceType")
        
        if source_type == "database":
            selected_table = request.form.get("table_name")
            flash(f"Analyzing signal from database table: {selected_table}", "success")
            
        else:
            f = request.files.get("csv_file")
            flash("Analyzing signal from uploaded file.", "success")
            
        # Redirect back or stay on page to show results
        return redirect(url_for("health_analysis"))

    
    return render_template(
        "health-analysis.html",
        datasets=DATASET_REGISTRY,
        date_columns=DATE_COLUMNS
    )



# route to handle grayscale conversion
@app.route("/grayscale", methods=["POST"])
def grayscale_image():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]

    image_bytes = np.frombuffer(file.read(), np.uint8)

    image = cv2.imdecode(image_bytes, cv2.IMREAD_COLOR)

    if image is None:
        return jsonify({"error": "Invalid image"}), 400

    #  GRAYSCALE CONVERSION 
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    pil_img = Image.fromarray(gray)

    img_io = BytesIO()
    pil_img.save(img_io, "PNG")
    img_io.seek(0)

    return send_file(img_io, mimetype="image/png")

BLUR_CACHE = {}

# route to handle image blurring
@app.route("/blur", methods=["POST"])
def blur_image():

    if "file" not in request.files:
        return "No file uploaded", 400

    file = request.files["file"]
    blur_type = request.form.get("blur_type", "gaussian")
    kernel = int(request.form.get("kernel", 5))

    # Ensure kernel is odd
    if kernel % 2 == 0:
        kernel += 1

    img = cv2.imdecode(
        np.frombuffer(file.read(), np.uint8),
        cv2.IMREAD_COLOR
    )

    if img is None:
        return "Invalid image", 400
    
    BLUR_CACHE["image"] = img

    if blur_type == "average":
        blurred = cv2.blur(img, (kernel, kernel))

    elif blur_type == "gaussian":
        blurred = cv2.GaussianBlur(img, (kernel, kernel), 0)

    elif blur_type == "median":
        blurred = cv2.medianBlur(img, kernel)

    elif blur_type == "bilateral":
        blurred = cv2.bilateralFilter(img, kernel, 75, 75)

    else:
        return "Unknown blur type", 400

    _, buffer = cv2.imencode(".png", blurred)
    return send_file(BytesIO(buffer), mimetype="image/png")

# real-time blur adjustment
@app.route("/blur/realtime", methods=["POST"])
def realtime_blur():
    if "image" not in BLUR_CACHE:
        return "No image loaded", 400

    blur_type = request.json.get("blur_type", "gaussian")
    kernel = int(request.json.get("kernel", 5))

    if kernel % 2 == 0:
        kernel += 1

    img = BLUR_CACHE["image"]

    if blur_type == "average":
        blurred = cv2.blur(img, (kernel, kernel))

    elif blur_type == "gaussian":
        blurred = cv2.GaussianBlur(img, (kernel, kernel), 0)

    elif blur_type == "median":
        blurred = cv2.medianBlur(img, kernel)

    elif blur_type == "bilateral":
        blurred = cv2.bilateralFilter(img, kernel, 75, 75)

    else:
        return "Unknown blur type", 400

    _, buffer = cv2.imencode(".png", blurred)
    return send_file(BytesIO(buffer), mimetype="image/png")



# route to handle image difference
@app.route('/difference', methods=['POST'])
def difference():
    # Check if two files were uploaded
    if 'image1' not in request.files or 'image2' not in request.files:
        return abort(400, description="Two images required")

    file1 = request.files['image1']
    file2 = request.files['image2']

    # Convert file streams to NumPy arrays
    img1 = cv2.imdecode(np.frombuffer(file1.read(), np.uint8), cv2.IMREAD_COLOR)
    img2 = cv2.imdecode(np.frombuffer(file2.read(), np.uint8), cv2.IMREAD_COLOR)

    if img1 is None or img2 is None:
        return abort(400, description="Invalid images uploaded")

    # Resize images to the same size if necessary
    if img1.shape != img2.shape:
        img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))

    # Perform difference operation
    diff = cv2.absdiff(img1, img2)

    # Save result to a temporary file to send as response
    _, temp_filename = tempfile.mkstemp(suffix=".png")
    cv2.imwrite(temp_filename, diff)

    return send_file(temp_filename, mimetype='image/png')

# route to handle image equalization
@app.route('/equalize', methods=['POST'])
def equalize():
    if 'file' not in request.files:
        abort(400, "No image uploaded")

    file = request.files['file']
    img = cv2.imdecode(np.frombuffer(file.read(), np.uint8), cv2.IMREAD_GRAYSCALE)

    if img is None:
        abort(400, "Invalid image")

    equalized = cv2.equalizeHist(img)

    # Encode directly to PNG in memory
    success, buffer = cv2.imencode(".png", equalized)
    if not success:
        abort(500, "Encoding failed")

    return send_file(
        io.BytesIO(buffer),
        mimetype="image/png"
    )

# manual thresholding
@app.route('/threshold', methods=['POST'])
def threshold():
    if 'file' not in request.files or 'value' not in request.form:
        abort(400, "Image and threshold value required")

    file = request.files['file']
    value = int(request.form['value'])

    img = cv2.imdecode(
        np.frombuffer(file.read(), np.uint8),
        cv2.IMREAD_GRAYSCALE
    )

    if img is None:
        abort(400, "Invalid image")

    _, thresh_img = cv2.threshold(
        img, value, 255, cv2.THRESH_BINARY
    )

    success, buffer = cv2.imencode(".png", thresh_img)
    if not success:
        abort(500, "Encoding failed")

    return send_file(io.BytesIO(buffer), mimetype="image/png")

# Otsu's thresholding
@app.route('/auto-threshold', methods=['POST'])
def auto_threshold():
    if 'file' not in request.files:
        return abort(400, description="No image uploaded")

    file = request.files['file']
    img = cv2.imdecode(np.frombuffer(file.read(), np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return abort(400, description="Invalid image")

    # Otsu threshold
    otsu_value, thresh_img = cv2.threshold(
        img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )

    # Save to temporary file
    _, temp_filename = tempfile.mkstemp(suffix=".png")
    cv2.imwrite(temp_filename, thresh_img)

    response = send_file(temp_filename, mimetype='image/png')

    
    response.headers['X-Otsu-Threshold'] = str(int(otsu_value))
    response.headers['Access-Control-Expose-Headers'] = 'X-Otsu-Threshold'

    return response

# route to handle border object removal
@app.route('/clear-border', methods=['POST'])
def clear_border_operation():
    if 'file' not in request.files:
        return abort(400, description="No image uploaded")

    file = request.files['file']
    img = cv2.imdecode(np.frombuffer(file.read(), np.uint8), cv2.IMREAD_GRAYSCALE)

    if img is None:
        return abort(400, description="Invalid image")

    # Binary image
    _, binary = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Clear border
    cleared = clear_border(binary)

    # Removed mask
    removed_mask = (binary > 0) & (cleared == 0)
    removed_count = label(removed_mask).max()

    # Overlay image
    overlay = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
    overlay[removed_mask] = [0, 0, 255]

    # Clean image
    clean_rgb = cv2.cvtColor(cleared, cv2.COLOR_GRAY2BGR)

    # Encode images
    _, buf1 = cv2.imencode('.png', overlay)
    _, buf2 = cv2.imencode('.png', clean_rgb)

    return {
        "overlay": base64.b64encode(buf1).decode(),
        "clean": base64.b64encode(buf2).decode(),
        "removed": int(removed_count)
    }

# edge detection
@app.route('/edge-detection', methods=['POST'])
def edge_detection():
    if 'file' not in request.files:
        return abort(400, description="No image uploaded")

    file = request.files['file']

    # Read image in grayscale
    img = cv2.imdecode(
        np.frombuffer(file.read(), np.uint8),
        cv2.IMREAD_GRAYSCALE
    )

    if img is None:
        return abort(400, description="Invalid image")

    #  CANNY EDGE DETECTION 
    edges = cv2.Canny(img, threshold1=100, threshold2=200)

    # Save to temporary file
    _, temp_filename = tempfile.mkstemp(suffix=".png")
    cv2.imwrite(temp_filename, edges)

    return send_file(temp_filename, mimetype="image/png")

SIGNAL_CACHE = {}

def parse_csv_signal(file_bytes: bytes):
    text = file_bytes.decode("utf-8", errors="ignore")
    data = np.genfromtxt(
        io.StringIO(text),
        delimiter=",",
        dtype=float,
        skip_header=1
    )

    if data.ndim == 1:
        data = data.reshape(-1, 1)

    # If 2+ columns assume [time, amplitude]
    if data.shape[1] >= 2:
        t = data[:, 0]
        x = data[:, 1]
        dt = np.median(np.diff(t)) if len(t) > 2 else None
        fs_est = (1.0 / dt) if (dt and dt > 0) else None
        return t, x, fs_est
    else:
        x = data[:, 0]
        return None, x, None
    
# route to load signal from CSV    
@app.route("/signal/load", methods=["POST"])
def load_signal():
    if "file" not in request.files:
        return abort(400, description="No CSV uploaded")

    f = request.files["file"]
    file_bytes = f.read()

    t, x, fs_est = parse_csv_signal(file_bytes)

    fs_in = request.form.get("fs", type=float)
    fs = fs_est if fs_est else fs_in

    if fs < 20:
        return jsonify({"error": "Sampling rate too low"}), 400


    if not fs or fs <= 0:
        return abort(400, description="Sampling rate (fs) is required or must be derivable from time column.")

    x = np.asarray(x, dtype=float)
    n = len(x)

    if t is None:
        t = np.arange(n) / fs
    else:
        t = np.asarray(t, dtype=float)
    
    duration_req = request.form.get("duration", type=float)  

    if duration_req and duration_req > 0:
        max_samples = int(duration_req * fs)

        # prevent empty/too-small slice
        max_samples = max(10, min(max_samples, len(x)))

        t = t[:max_samples]
        x = x[:max_samples]

        # make time start at 0 for clean display (optional but nice)
        t = t - t[0]

    # Store in cache
    signal_id = str(uuid.uuid4())
    SIGNAL_CACHE[signal_id] = {
        "x_original": x.copy(),
        "x": x.copy(),
        "t": t,
        "fs": fs,
        "noise": None
    }


    n = len(x) 
    
    return jsonify({
        "signal_id": signal_id,
        "fs": round(float(fs), 3),
        "duration": float(n / fs),
        "t": t.tolist(),
        "x": x.tolist(),
        
        "x_original": SIGNAL_CACHE[signal_id]["x_original"].tolist()
    })

# route fft processing
@app.route("/signal/fft", methods=["POST"])
def compute_fft():
    data = request.get_json(force=True)

    signal_id = data.get("signal_id")
    if not signal_id or signal_id not in SIGNAL_CACHE:
        return abort(400, description="Invalid or missing signal_id")
    
    signal_id = data.get("signal_id")
    if signal_id not in SIGNAL_CACHE:
        return abort(400, description="Signal not found. Load a signal first.")

    win_start = float(data.get("window_start", 0.0))
    win_len   = float(data.get("window_length", 4.0))
    nfft      = int(data.get("nfft", 1024))
    peak_band = data.get("peak_band_hz", [0.7, 3.5])  

    if win_len <= 0:
        return abort(400, description="Window length must be > 0")
    if nfft < 16:
        return abort(400, description="nfft too small")

    x = SIGNAL_CACHE[signal_id]["x"]
    t = SIGNAL_CACHE[signal_id]["t"]
    fs = SIGNAL_CACHE[signal_id]["fs"]

    start_idx = int(round(win_start * fs))
    end_idx   = int(round((win_start + win_len) * fs))

    start_idx = max(0, min(start_idx, len(x) - 1))
    end_idx   = max(start_idx + 1, min(end_idx, len(x)))

    segment = x[start_idx:end_idx]
    if len(segment) < 4:
        return abort(400, description="Selected window is too short.")
    
    w = np.hanning(len(segment))
    seg_w = segment * w

    seg_w = seg_w - np.mean(seg_w)

    X = np.fft.rfft(seg_w, n=nfft)
    freq = np.fft.rfftfreq(nfft, d=1.0/fs)

    power = (np.abs(X) ** 2) / nfft
    power = np.maximum(power, 1e-15)

    # Peak detection in band
    band_lo, band_hi = float(peak_band[0]), float(peak_band[1])
    mask = (freq >= band_lo) & (freq <= band_hi)

    peak_hz = None
    hr_bpm = None
    if np.any(mask):
        idx = np.argmax(power[mask])
        peak_hz = float(freq[mask][idx])
        hr_bpm = float(peak_hz * 60.0)

    return jsonify({
        "freq": freq.tolist(),
        "power": power.tolist(),
        "peak_hz": peak_hz,
        "hr_bpm": hr_bpm,
        "time": {"t": t.tolist(), "x": x.tolist()},
        "window": {"start_idx": start_idx, "end_idx": end_idx, "fs": float(fs)}
    })


# route for noise injection
@app.route("/signal/add-noise", methods=["POST"])
def add_noise():
    data = request.get_json(force=True)
    signal_id = data.get("signal_id")
    if not signal_id or signal_id not in SIGNAL_CACHE:
        return abort(400, description="Invalid or missing signal_id")

    noise_type = (data.get("noise_type") or "gaussian").lower()
    noise_level = float(data.get("noise_level", 0.0))
    if noise_level <= 0:
        return abort(400, description="noise_level must be > 0")

    fs = float(SIGNAL_CACHE[signal_id]["fs"])
    t = SIGNAL_CACHE[signal_id]["t"]
    x_orig = SIGNAL_CACHE[signal_id]["x_original"]

    x_new = x_orig.copy()

    seed = data.get("seed", None)
    rng = np.random.default_rng(seed) if seed is not None else np.random.default_rng()

    if noise_type == "gaussian":
        x_new = x_new + rng.normal(0.0, noise_level, size=x_new.shape)

    elif noise_type == "powerline":
        f0 = 50.0
        x_new = x_new + noise_level * np.sin(2 * np.pi * f0 * t)

    else:
        return abort(400, description="Unsupported noise_type. Use gaussian or powerline.")

    # update current signal
    SIGNAL_CACHE[signal_id]["x"] = x_new
    SIGNAL_CACHE[signal_id]["noise"] = {"type": noise_type, "level": noise_level}

    return jsonify({
        "signal_id": signal_id,
        "t": t.tolist(),
        "x_noisy": x_new.tolist(),
        "x_original": x_orig.tolist(),
        "noise": SIGNAL_CACHE[signal_id]["noise"]

    })

# reset signal
@app.route("/signal/reset", methods=["POST"])
def reset_signal():
    data = request.get_json(force=True)
    signal_id = data.get("signal_id")
    if not signal_id or signal_id not in SIGNAL_CACHE:
        return abort(400, description="Invalid or missing signal_id")

    t = SIGNAL_CACHE[signal_id]["t"]
    x_orig = SIGNAL_CACHE[signal_id]["x_original"].copy()

    SIGNAL_CACHE[signal_id]["x"] = x_orig
    SIGNAL_CACHE[signal_id]["noise"] = None

    return jsonify({
        "signal_id": signal_id,
        "t": t.tolist(),
        "x": x_orig.tolist()
    })

# filter signal route
@app.route("/signal/filter", methods=["POST"])
def apply_filter():
    data = request.get_json()
    s_id = data['signal_id']
    f_type = data['filter_type']
    order = data['order']
    fs = SIGNAL_CACHE[s_id]['fs']
    nyq = 0.5 * fs
    
    if f_type in ['highpass', 'bandstop'] and order % 2 == 0:
        order += 1

    # Define cutoffs
    c1 = data['cutoff1'] / nyq
    if f_type in ['bandpass', 'bandstop']:
        c2 = data['cutoff2'] / nyq
        taps = firwin(order, [c1, c2], pass_zero=(f_type == 'bandstop'))
    else:
        taps = firwin(order, c1, pass_zero=(f_type == 'lowpass'))

    # Apply filter to the CURRENT (noisy) signal
    x_noisy = SIGNAL_CACHE[s_id]['x']
    x_filtered = lfilter(taps, 1.0, x_noisy)
    
    # Update cache so FFT reflects filtered signal
    SIGNAL_CACHE[s_id]['x'] = x_filtered
    
    return jsonify({"x_filtered": x_filtered.tolist()})

# route for health analysis table metadata
@app.route("/get-metadata/<table_name>")
def get_metadata(table_name):
    if table_name not in DATASET_REGISTRY:
        return jsonify({"error": "Invalid table"}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Get Row Count
    cursor.execute(f"SELECT COUNT(*) AS total FROM `{table_name}`")
    total_rows = cursor.fetchone()["total"]
    
    # Get Column Names
    cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
    col_names = [row["Field"] for row in cursor.fetchall()]
    total_cols = len(col_names)
    
    cursor.close()
    conn.close()

    return jsonify({
        "name": table_name,
        "row_count": total_rows,
        "col_names": col_names,
        "cols_count": total_cols
    })

# moving average helper
def moving_average(x: np.ndarray, window: int) -> np.ndarray:
    window = int(window)
    if window <= 1:
        return x
    kernel = np.ones(window, dtype=float) / window
    return np.convolve(x, kernel, mode="same")

# local data helper
def ensure_local_record_exists():
    hea = str(LOCAL_RECORD_BASE) + ".hea"
    dat = str(LOCAL_RECORD_BASE) + ".dat"

    print("CWD:", os.getcwd())
    print("Checking:", str(LOCAL_RECORD_BASE) + ".hea")
    print("Checking:", str(LOCAL_RECORD_BASE) + ".dat")


    if not (os.path.exists(hea) and os.path.exists(dat)):
        raise FileNotFoundError(
            f"Missing WFDB files. Expected both:\n- {hea}\n- {dat}"
        )


# helper for outlier removal
def remove_outliers_iqr(x: np.ndarray, k: float = 1.5) -> np.ndarray:
    """
    Clip values outside [Q1 - k*IQR, Q3 + k*IQR].
    This is robust and works well for ECG demo.
    """
    q1, q3 = np.percentile(x, [25, 75])
    iqr = q3 - q1
    if iqr == 0:
        return x
    lower = q1 - k * iqr
    upper = q3 + k * iqr
    return np.clip(x, lower, upper)

# oulier removal route
@app.post("/api/filter/remove-outliers")
def api_filter_remove_outliers():
    """
    Body: { raw: [...], method: "iqr", k: 1.5 }
    Returns: filtered signal (outliers clipped)
    """
    try:
        payload = request.get_json(force=True)
        raw = np.array(payload.get("raw", []), dtype=float)
        method = (payload.get("method") or "iqr").lower()
        k = float(payload.get("k", 1.5))

        if raw.size == 0:
            return jsonify(success=False, message="No raw data provided."), 400

        if method == "iqr":
            filtered = remove_outliers_iqr(raw, k=k)
        else:
            return jsonify(success=False, message=f"Unknown method: {method}"), 400

        return jsonify(success=True, filtered=filtered.tolist(), method=method, k=k)

    except Exception as e:
        return jsonify(success=False, message=str(e)), 500


# ECG demo route
@app.get("/api/ecg/demo")
def api_ecg_demo():
    try:
        ensure_local_record_exists()

        lead_name = request.args.get("lead", "II")
        sampfrom = int(request.args.get("sampfrom", 0))
        requested_sampto = int(request.args.get("sampto", 2000))

        header = wfdb.rdheader(str(LOCAL_RECORD_BASE))
        sig_len = int(header.sig_len) 
        fs = float(header.fs)
        
        print("sig_len:", sig_len, "fs:", fs)

        if sampfrom < 0:
            sampfrom = 0
        if sampfrom >= sig_len:
            return jsonify(success=False, message=f"sampfrom={sampfrom} is out of range (0..{sig_len-1})"), 400

        sampto = min(requested_sampto, sig_len)
        if sampto <= sampfrom:
            sampto = min(sampfrom + 1, sig_len)

        # reading the actual segment
        rec = wfdb.rdrecord(str(LOCAL_RECORD_BASE), sampfrom=sampfrom, sampto=sampto)

        sig_names = rec.sig_name
        if lead_name not in sig_names:
            return jsonify(success=False, message=f"Lead '{lead_name}' not found. Available: {sig_names}"), 400

        idx = rec.sig_name.index(lead_name)
        x = (rec.p_signal[:, idx] if rec.p_signal is not None else rec.d_signal[:, idx]).astype(float)

        print("RAW ECG samples:", x[:10])

        return jsonify(
            success=True,
            fs=fs,
            lead=lead_name,
            sig_names=sig_names,
            raw=x.tolist(),
            sampfrom=sampfrom,
            sampto=sampto,
            sig_len=sig_len
        )

    except Exception as e:
        return jsonify(success=False, message=str(e)), 500

# moving average route
@app.post("/api/filter/moving-average")
def api_filter_moving_average():
    """
    Body: { raw: [...], window: 5 }
    """
    try:
        payload = request.get_json(force=True)
        raw = np.array(payload.get("raw", []), dtype=float)
        window = int(payload.get("window", 5))

        if raw.size == 0:
            return jsonify(success=False, message="No raw data provided."), 400

        filtered = moving_average(raw, window)
        return jsonify(success=True, filtered=filtered.tolist(), window=window)

    except Exception as e:
        return jsonify(success=False, message=str(e)), 500



# correlation route
def load_table_as_dataframe(table_name):
    """
    Load a MySQL table into a pandas DataFrame
    using your existing DB connection logic.
    """
    if table_name not in DATASET_REGISTRY:
        raise ValueError("Invalid table name")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute(f"SELECT * FROM `{table_name}`")
    rows = cursor.fetchall()

    cursor.close()
    conn.close()

    # Convert list of dicts to DataFrame
    df = pd.DataFrame(rows)

    return df


# clean data in dataframe
def clean_metadata_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Clean common metadata columns used for correlation.
    - Convert to numeric
    - Treat 0 or negative as missing for height/weight/bmi
    - Remove extreme/unrealistic values (optional safe bounds)
    """

    # Convert key columns to numeric if they exist
    for col in ["age", "height", "weight", "sex"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Clean height (assume cm if values are like 150–200)
    if "height" in df.columns:
        # treat 0/negative as missing
        df.loc[df["height"] <= 0, "height"] = np.nan
        # optional realistic bounds for adults (cm)
        df.loc[(df["height"] < 50) | (df["height"] > 250), "height"] = np.nan

    # Clean weight (kg)
    if "weight" in df.columns:
        df.loc[df["weight"] <= 0, "weight"] = np.nan
        # optional realistic bounds
        df.loc[(df["weight"] < 10) | (df["weight"] > 300), "weight"] = np.nan

    # Clean age (years)
    if "age" in df.columns:
        df.loc[df["age"] <= 0, "age"] = np.nan
        df.loc[(df["age"] < 0) | (df["age"] > 120), "age"] = np.nan

    # sex is 0/1 already, but make sure it is only 0/1
    if "sex" in df.columns:
        df.loc[~df["sex"].isin([0, 1]), "sex"] = np.nan

    return df


# populate column
@app.get("/api/metadata/columns")
def api_metadata_columns():
    try:
        table = request.args.get("table")
        if not table:
            return jsonify(success=False, message="Missing table"), 400

        df = load_table_as_dataframe(table)

        allowed = ["age", "trestbps", "chol", "thalach", "oldpeak", "height", "weight"]

        # Keep only those that exist in the dataframe
        cols = [c for c in allowed if c in df.columns]

        # Add bmi (lowercase) as a derived field if possible
        if "height" in df.columns and "weight" in df.columns:
            cols.append("bmi")

        return jsonify(success=True, columns=cols)

    except Exception as e:
        return jsonify(success=False, message=str(e)), 500


    
# correlation operation route
@app.get("/api/metadata/correlation")
def api_metadata_correlation():
    try:
        table = request.args.get("table")
        xcol = request.args.get("x")
        ycol = request.args.get("y")

        if not table or not xcol or not ycol:
            return jsonify(success=False, message="Missing parameters"), 400

        df = load_table_as_dataframe(table)
        df = clean_metadata_df(df)

        # Add BMI if requested
        if "bmi" in (xcol, ycol):
            if "height" not in df.columns or "weight" not in df.columns:
                return jsonify(success=False, message="bmi requires height and weight"), 400

            h = pd.to_numeric(df["height"], errors="coerce")
            w = pd.to_numeric(df["weight"], errors="coerce")

            # Height in cm = m
            h_m = np.where(h > 3, h / 100.0, h)

            df["bmi"] = w / (h_m ** 2)
            df.loc[(h_m <= 0) | (~np.isfinite(df["bmi"])) | (df["bmi"] <= 0), "bmi"] = np.nan

        # Keep only numeric values
        df = df[[xcol, ycol]].apply(pd.to_numeric, errors="coerce").dropna()

        if len(df) < 3:
            return jsonify(success=False, message="Not enough data points"), 400

        r = float(df[xcol].corr(df[ycol], method="pearson"))

        return jsonify(
            success=True,
            r=r,
            n=len(df),
            x=xcol,
            y=ycol,
            x_values=df[xcol].tolist(),
            y_values=df[ycol].tolist()
        )

    except Exception as e:
        return jsonify(success=False, message=str(e)), 500

# heatmap
@app.get("/api/metadata/corr-matrix")
def api_metadata_corr_matrix():
    try:
        table = request.args.get("table")
        cols = request.args.getlist("cols")

        if not table:
            return jsonify(success=False, message="Missing table"), 400

        df = load_table_as_dataframe(table)
        df = clean_metadata_df(df)


        # If no columns specified, auto-pick numeric ones
        if not cols:
            preferred = ["age", "trestbps", "chol", "thalach", "oldpeak", "height", "weight", "bmi"]
            cols = [c for c in preferred if c in df.columns]

        # Add BMI if requested
        if "bmi" in cols:
            if "height" not in df.columns or "weight" not in df.columns:
                return jsonify(success=False, message="bmi requires height and weight"), 400

            h = pd.to_numeric(df["height"], errors="coerce")
            w = pd.to_numeric(df["weight"], errors="coerce")
            h_m = np.where(h > 3, h / 100.0, h)

            df["bmi"] = w / (h_m ** 2)
            df.loc[(h_m <= 0) | (~np.isfinite(df["bmi"])) | (df["bmi"] <= 0), "bmi"] = np.nan


        df = df[cols].apply(pd.to_numeric, errors="coerce").dropna()

        if len(df) < 3:
            return jsonify(success=False, message="Not enough rows for correlation matrix"), 400

        corr = df.corr(method="pearson")

        return jsonify(
            success=True,
            columns=cols,
            matrix=corr.values.tolist()
        )

    except Exception as e:
        return jsonify(success=False, message=str(e)), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
