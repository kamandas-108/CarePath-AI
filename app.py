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

    system_prompt = (
        f"You are a healthcare assistant. Analyze symptoms strictly for educational awareness.\n"
        f"Never provide a definitive medical diagnosis. Always advise consulting healthcare professionals.\n"
        f"Target response translation language: {language}.\n"
        f"If the incoming symptom input language matches Hindi, Odia, or Bengali, read it comfortably and respond in that targeted script or language choice requested.\n\n"
        f"Analyze the following user input: \"{symptoms}\".{history_context}\n\n"
        f"CRITICAL: If historical context shows an escalating trend (e.g. fever going up across entries or spreading pain), explicitly note this in the Health Education text.\n\n"
        f"You must respond ONLY with a clean, raw JSON block. Do not format it inside
