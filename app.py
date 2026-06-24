import os
import json
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from dotenv import load_dotenv
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime

# Load environment variables
load_dotenv()

app = Flask(__name__, template_folder='.')
app.secret_key = os.getenv("FLASK_SECRET_KEY", "carepath_ai_default_secret_2026")

# Configure Gemini SDK
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    print("Warning: GEMINI_API_KEY is not set.")

# Database Connection Helper
def get_db_connection():
    connection_string = os.getenv("NEON_DB_STRING")
    if not connection_string:
        raise ValueError("NEON_DB_STRING configuration missing!")
    return psycopg2.connect(connection_string, cursor_factory=RealDictCursor)

# Initialize Database Schema
def init_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Create Users table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password VARCHAR(100) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Create Health Tracker / Symptom History table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS health_logs (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                symptoms TEXT NOT NULL,
                ai_response TEXT NOT NULL,
                risk_level VARCHAR(20) NOT NULL,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Database initialization error: {e}")

# Run DB Initialization
init_db()

# Main Entry Route
@app.route('/')
def index():
    return render_template('index.html')

# --- AUTHENTICATION ENDPOINTS ---
@app.route('/api/auth/signup', methods=['POST'])
def signup():
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    if not username or not password:
        return jsonify({"success": False, "message": "Missing credentials"}), 400

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE username = %s;", (username,))
        if cur.fetchone():
            return jsonify({"success": False, "message": "Username already exists"}), 400
        
        cur.execute("INSERT INTO users (username, password) VALUES (%s, %s) RETURNING id, username;", (username, password))
        user = cur.fetchone()
        conn.commit()
        
        session['user_id'] = user['id']
        session['username'] = user['username']
        
        cur.close()
        conn.close()
        return jsonify({"success": True, "user": {"id": user['id'], "username": user['username']}})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, username, password FROM users WHERE username = %s;", (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()

        if user and user['password'] == password: # Simple plaintext matching for demo/hackathon deployment
            session['user_id'] = user['id']
            session['username'] = user['username']
            return jsonify({"success": True, "user": {"id": user['id'], "username": user['username']}})
        else:
            return jsonify({"success": False, "message": "Invalid username or password"}), 401
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"success": True, "message": "Logged out successfully"})

@app.route('/api/auth/status', methods=['GET'])
def auth_status():
    if 'user_id' in session:
        return jsonify({"logged_in": True, "username": session['username']})
    return jsonify({"logged_in": False})


# --- AI ANALYSIS ENGINE ENDPOINT ---
@app.route('/api/analyze', methods=['POST'])
def analyze_symptoms():
    data = request.get_json() or {}
    symptoms = data.get('symptoms', '').strip()
    language = data.get('language', 'English').strip()

    if not symptoms:
        return jsonify({"success": False, "message": "No symptoms provided."}), 400

    # Read historical background context for user if logged in
    history_context = ""
    if 'user_id' in session:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT symptoms, risk_level, logged_at FROM health_logs WHERE user_id = %s ORDER BY logged_at DESC LIMIT 3;", (session['user_id'],))
            logs = cur.fetchall()
            cur.close()
            conn.close()
            if logs:
                history_context = "\nRecent User Health Logs (for tracking trend changes over days):\n"
                for l in logs:
                    history_context += f"- Date: {l['logged_at'].strftime('%Y-%m-%d')}, Symptoms: {l['symptoms']}, Risk Level: {l['risk_level']}\n"
        except Exception as e:
            print(f"Error fetching user logs: {e}")

    # Triple-quote syntax fixes the string literal line-break error
    system_prompt = f"""You are a healthcare assistant. Analyze symptoms strictly for educational awareness.
Never provide a definitive medical diagnosis. Always advise consulting healthcare professionals.
Target response translation language: {language}.
If the incoming symptom input language matches Hindi, Odia, or Bengali, read it comfortably and respond in that targeted script or language choice requested.

Analyze the following user input: "{symptoms}".{history_context}

CRITICAL: If historical context shows an escalating trend (e.g. fever going up across entries or spreading pain), explicitly note this in the Health Education text.

You must respond ONLY with a clean, raw JSON block. Do not format it inside markdown syntax blocks. Ensure it matches this exact structure keys:
{{
  "conditions": [
    {{"name": "Condition Name", "confidence": "Percentage%"}}
  ],
  "risk_level": "Low" or "Medium" or "High",
  "recommendation": "Specific service recommendation structure based on system guidelines (e.g., Home Care, General Physician, Specialist, or Emergency Room)",
  "education": "Simple educational explanation of conditions, including tracking trends if relevant.",
  "warnings": "Emergency warning signs requiring immediate action."
}}"""

    try:
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content(system_prompt)
        text_response = response.text.strip()
        
        # Strip potential markdown syntax wrapping code blocks if Gemini accidentally provides them
        if text_response.startswith("```"):
            text_response = text_response.replace("```json", "").replace("```", "").strip()

        parsed_json = json.loads(text_response)

        # Save to database if user is logged in
        if 'user_id' in session:
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO health_logs (user_id, symptoms, ai_response, risk_level) VALUES (%s, %s, %s, %s);",
                    (session['user_id'], symptoms, json.dumps(parsed_json), parsed_json.get('risk_level', 'Low'))
                )
                conn.commit()
                cur.close()
                conn.close()
            except Exception as ex:
                print(f"Failed to log entry: {ex}")

        return jsonify({"success": True, "data": parsed_json})

    except Exception as e:
        return jsonify({"success": False, "message": f"AI Processing Error: {str(e)}"}), 500


# --- HEALTH LOGS / TRACKER HISTORICAL TIMELINE ---
@app.route('/api/history', methods=['GET'])
def get_history():
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT symptoms, ai_response, risk_level, logged_at FROM health_logs WHERE user_id = %s ORDER BY logged_at DESC;", (session['user_id'],))
        logs = cur.fetchall()
        cur.close()
        conn.close()
        
        formatted_logs = []
        for l in logs:
            formatted_logs.append({
                "symptoms": l['symptoms'],
                "ai_response": json.loads(l['ai_response']),
                "risk_level": l['risk_level'],
                "logged_at": l['logged_at'].strftime('%Y-%m-%d %H:%M:%S')
            })
        return jsonify({"success": True, "logs": formatted_logs})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
