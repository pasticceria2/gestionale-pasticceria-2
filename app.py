
import os
import calendar
import re
import base64
import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, time, timedelta, timezone, date
try:
    from zoneinfo import ZoneInfo
    ROME_TZ = ZoneInfo("Europe/Rome")
except Exception:
    ROME_TZ = None

def now_italy():
    """Restituisce il datetime corrente nel fuso orario di Roma (Europe/Rome)."""
    if ROME_TZ:
        try:
            return datetime.now(ROME_TZ)
        except Exception:
            pass
    return datetime.now()

import json
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from werkzeug.security import generate_password_hash, check_password_hash

def process_uploaded_image(file_obj):
    """Converte un file immagine inviato via form in stringa Base64 per la memorizzazione su DB."""
    if not file_obj or file_obj.filename == '':
        return None
    try:
        file_bytes = file_obj.read()
        if not file_bytes:
            return None
        encoded = base64.b64encode(file_bytes).decode('utf-8')
        mimetype = file_obj.mimetype or 'image/jpeg'
        return f"data:{mimetype};base64,{encoded}"
    except Exception as e:
        print(f"Errore caricamento foto: {e}")
        return None


app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'una-chiave-segreta-molto-sicura-da-cambiare')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin')

@app.context_processor
def inject_user_context():
    perms = session.get('permissions') or {}
    # Default is_schiavoni è True se non specificato altrimenti
    is_schiavoni = perms.get('is_schiavoni', True)
    ferie_in_attesa_count = 0
    if 'user_id' in session:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS c FROM piano_ferie WHERE confermato = FALSE AND COALESCE(rifiutato, FALSE) = FALSE")
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                ferie_in_attesa_count = row['c'] or 0
        except Exception:
            pass
    return dict(is_schiavoni=is_schiavoni, user_permissions=perms, ferie_in_attesa_count=ferie_in_attesa_count)

# --- DECORATORI E FUNZIONI DI UTILITÀ PER L'AUTENTICAZIONE ---

def admin_required(f):
    """Decoratore: richiede solo che l'utente sia loggato (l'accesso admin è protetto da password in conferma_admin)."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Accesso non autorizzato.", "danger")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def permission_required(permission_name):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('login'))
            # Se l'utente ha admin_verified (ha passato la verifica con password), ha tutti i permessi
            if session.get('admin_verified'):
                return f(*args, **kwargs)
            perms = session.get('permissions') or {}
            if not perms.get(permission_name, True):
                flash("Non hai i permessi per accedere a questa funzione.", "danger")
                return redirect(url_for('produzione'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def get_db_connection():
    return psycopg2.connect(os.environ.get('DATABASE_URL'), cursor_factory=RealDictCursor)

def save_or_update_client(cur, nome, telefono):
    """Aggiunge il cliente in anagrafica se non presente, lasciando inalterato se già esistente."""
    if not nome or not nome.strip():
        return
    nome_clean = nome.strip()
    tel_clean = telefono.strip() if telefono and telefono.strip() else None

    if tel_clean:
        cur.execute("""
            INSERT INTO clienti (nome, telefono) 
            VALUES (%s, %s) 
            ON CONFLICT (telefono) DO NOTHING
        """, (nome_clean, tel_clean))
    else:
        cur.execute("SELECT id FROM clienti WHERE LOWER(nome) = LOWER(%s)", (nome_clean,))
        if not cur.fetchone():
            cur.execute("INSERT INTO clienti (nome, telefono) VALUES (%s, NULL)", (nome_clean,))

_db_initialized = False

def init_db():
    global _db_initialized
    if os.environ.get('DATABASE_URL'):
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # Creazione tabella utenti
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    permissions JSONB DEFAULT '{"can_edit": true, "can_delete": true, "can_manage_users": false, "can_view_anagrafica": true}'::jsonb
                );
            """)
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS permissions JSONB DEFAULT '{\"can_edit\": true, \"can_delete\": true, \"can_manage_users\": false, \"can_view_anagrafica\": true}'::jsonb;")

            # Tabella per migrazioni di sistema e reset una-tantum
            cur.execute("""
                CREATE TABLE IF NOT EXISTS system_migrations (
                    name TEXT PRIMARY KEY,
                    executed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
            """)

            # Reset utenze: elimina tutte le utenze e crea utente Emanuele con tutti i permessi
            cur.execute("SELECT name FROM system_migrations WHERE name = 'reset_users_emanuele_v2';")
            if cur.fetchone() is None:
                cur.execute("DELETE FROM users;")
                emanuele_hash = generate_password_hash('Emanuele26@')
                emanuele_perms = '{"can_edit": true, "can_delete": true, "can_manage_users": true, "can_view_anagrafica": true, "is_schiavoni": true}'
                cur.execute("INSERT INTO users (username, password_hash, permissions) VALUES (%s, %s, %s)", ('Emanuele', emanuele_hash, emanuele_perms))
                cur.execute("INSERT INTO system_migrations (name) VALUES ('reset_users_emanuele_v2');")
            else:
                cur.execute("SELECT id FROM users LIMIT 1;")
                if cur.fetchone() is None:
                    emanuele_hash = generate_password_hash('Emanuele26@')
                    emanuele_perms = '{"can_edit": true, "can_delete": true, "can_manage_users": true, "can_view_anagrafica": true, "is_schiavoni": true}'
                    cur.execute("INSERT INTO users (username, password_hash, permissions) VALUES (%s, %s, %s)", ('Emanuele', emanuele_hash, emanuele_perms))

            cur.execute("CREATE TABLE IF NOT EXISTS clienti (id SERIAL PRIMARY KEY, nome TEXT, telefono TEXT UNIQUE, email TEXT, foto TEXT);")
            cur.execute("ALTER TABLE clienti ADD COLUMN IF NOT EXISTS email TEXT;")
            cur.execute("ALTER TABLE clienti ADD COLUMN IF NOT EXISTS foto TEXT;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ordini (
                    id SERIAL PRIMARY KEY, cliente TEXT, telefono TEXT, negozio TEXT, tipo_dolce TEXT, 
                    farcitura TEXT, kg DECIMAL, unita_misura TEXT, orario TIME, data_consegna DATE, 
                    descrizione TEXT, categoria TEXT, operatore TEXT, canale TEXT, 
                    modalita TEXT, indirizzo TEXT,
                    pagamento_tipo TEXT, pagamento_importo DECIMAL
                );
            """)
            cur.execute("ALTER TABLE ordini ADD COLUMN IF NOT EXISTS farcitura TEXT; UPDATE ordini SET farcitura = '' WHERE farcitura IS NULL;")
            cur.execute("ALTER TABLE ordini ADD COLUMN IF NOT EXISTS descrizione TEXT; UPDATE ordini SET descrizione = '' WHERE descrizione IS NULL;")
            cur.execute("ALTER TABLE ordini ADD COLUMN IF NOT EXISTS stampato BOOLEAN DEFAULT FALSE;")
            cur.execute("ALTER TABLE ordini ADD COLUMN IF NOT EXISTS foto TEXT;")
            cur.execute("ALTER TABLE ordini ADD COLUMN IF NOT EXISTS foto_descrizione TEXT;")
            cur.execute("ALTER TABLE ordini ADD COLUMN IF NOT EXISTS stampato_da TEXT;")
            cur.execute("ALTER TABLE ordini ADD COLUMN IF NOT EXISTS stampato_il TIMESTAMP WITH TIME ZONE;")
            cur.execute("ALTER TABLE ordini ADD COLUMN IF NOT EXISTS print_log JSONB DEFAULT '[]'::jsonb;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS supporto_notes (
                    id SERIAL PRIMARY KEY,
                    testo TEXT NOT NULL,
                    autore TEXT,
                    data_creazione TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("ALTER TABLE supporto_notes ADD COLUMN IF NOT EXISTS autore TEXT;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS piano_ferie (
                    id SERIAL PRIMARY KEY,
                    nome TEXT NOT NULL,
                    sede TEXT NOT NULL,
                    data_inizio DATE NOT NULL,
                    data_fine DATE NOT NULL,
                    note TEXT,
                    creato_da TEXT,
                    creato_il TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    confermato BOOLEAN DEFAULT FALSE,
                    confermato_da TEXT,
                    confermato_il TIMESTAMP WITH TIME ZONE
                );
            """)
            cur.execute("ALTER TABLE piano_ferie ADD COLUMN IF NOT EXISTS confermato BOOLEAN DEFAULT FALSE;")
            cur.execute("ALTER TABLE piano_ferie ADD COLUMN IF NOT EXISTS confermato_da TEXT;")
            cur.execute("ALTER TABLE piano_ferie ADD COLUMN IF NOT EXISTS confermato_il TIMESTAMP WITH TIME ZONE;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS turni_config (
                    anno INTEGER NOT NULL,
                    mese INTEGER NOT NULL,
                    martedi_aperto BOOLEAN DEFAULT FALSE,
                    barbara_inclusa BOOLEAN DEFAULT FALSE,
                    PRIMARY KEY (anno, mese)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS turni_assegnazioni (
                    id SERIAL PRIMARY KEY,
                    data DATE NOT NULL,
                    turno_id TEXT NOT NULL,
                    nome TEXT DEFAULT '',
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE (data, turno_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS turni_palocco_config (
                    anno INTEGER NOT NULL,
                    mese INTEGER NOT NULL,
                    martedi_aperto BOOLEAN DEFAULT FALSE,
                    PRIMARY KEY (anno, mese)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS turni_palocco_assegnazioni (
                    id SERIAL PRIMARY KEY,
                    data DATE NOT NULL,
                    turno_id TEXT NOT NULL,
                    nome TEXT DEFAULT '',
                    label TEXT DEFAULT '',
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE (data, turno_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS allergeni_prodotti (
                    id SERIAL PRIMARY KEY,
                    titolo TEXT NOT NULL,
                    ingredienti TEXT DEFAULT '',
                    allergeni JSONB DEFAULT '[]'::jsonb,
                    tracce TEXT DEFAULT '',
                    note TEXT DEFAULT '',
                    creato_il TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    aggiornato_il TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("ALTER TABLE allergeni_prodotti ADD COLUMN IF NOT EXISTS tracce TEXT DEFAULT '';")
            cur.execute("ALTER TABLE allergeni_prodotti ADD COLUMN IF NOT EXISTS note TEXT DEFAULT '';")
            conn.commit()
            _db_initialized = True
        except Exception as e: 
            print(f"Errore DB: {e}")
            conn.rollback()
        finally:
            cur.close()
            conn.close()

# Inizializza il DB all'avvio del modulo (WSGI / Gunicorn)
try:
    init_db()
except Exception as _e:
    print(f"Inizializzazione DB rinviata: {_e}")

@app.route('/fix_db')
def fix_db_route():
    try:
        init_db()
        return "✅ Database aggiornato con successo!", 200
    except Exception as e:
        return f"❌ Errore aggiornamento DB: {e}", 500

@app.route('/reset_users')
def reset_users_route():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM users;")
        emanuele_hash = generate_password_hash('Emanuele26@')
        emanuele_perms = '{"can_edit": true, "can_delete": true, "can_manage_users": true, "can_view_anagrafica": true, "is_schiavoni": true}'
        cur.execute("INSERT INTO users (username, password_hash, permissions) VALUES (%s, %s, %s)", ('Emanuele', emanuele_hash, emanuele_perms))
        cur.execute("INSERT INTO system_migrations (name) VALUES ('reset_users_emanuele_v2') ON CONFLICT (name) DO NOTHING;")
        conn.commit()
        cur.close()
        conn.close()
        return "✅ Tutte le utenze sono state resettate! È presente solo: username 'Emanuele' con password 'Emanuele26@'. Ora puoi creare le nuove utenze.", 200
    except Exception as e:
        return f"❌ Errore reset utenze: {e}", 500

# --- ROTTE DI AUTENTICAZIONE ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session: return redirect(url_for('produzione'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            perms = user.get('permissions') or {}
            if isinstance(perms, str):
                import json
                perms = json.loads(perms)
            session['permissions'] = perms
            return redirect(url_for('produzione'))
        else:
            flash('Credenziali non valide. Riprova.', 'danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Sei stato disconnesso.', 'success')
    return redirect(url_for('login'))

@app.before_request
def require_login():
    global _db_initialized
    if not _db_initialized:
        try:
            init_db()
        except Exception as e:
            print(f"Errore init_db in before_request: {e}")
    allowed_routes = ['login', 'static', 'verifica_admin', 'fix_db', 'reset_users']
    if request.endpoint not in allowed_routes and 'user_id' not in session:
        return redirect(url_for('login'))
    
    # Se l'utente è loggato ma manca lo username nella sessione (es. sessioni precedenti), recuperalo dal DB
    if 'user_id' in session and not session.get('username'):
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT username, permissions FROM users WHERE id = %s", (session['user_id'],))
            u = cur.fetchone()
            cur.close()
            conn.close()
            if u:
                session['username'] = u['username']
                if not session.get('permissions'):
                    perms = u.get('permissions') or {}
                    if isinstance(perms, str):
                        import json
                        perms = json.loads(perms)
                    session['permissions'] = perms
        except Exception as e:
            print(f"Errore recupero utente sessione: {e}")

# --- ROTTE GESTIONE UTENTI ---
@app.route('/conferma_admin', methods=['GET'])
@admin_required
def conferma_admin():
    return render_template('conferma_admin.html')

@app.route('/verifica_admin', methods=['POST'])
@admin_required
def verifica_admin():
    password_inserita = request.form.get('admin_password')
    user_id = session.get('user_id')
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT password_hash FROM users WHERE id = %s", (user_id,))
    user = cur.fetchone()
    cur.close()
    conn.close()

    is_user_pass_correct = user and check_password_hash(user['password_hash'], password_inserita)
    is_admin_pass_correct = (password_inserita == ADMIN_PASSWORD)

    if is_user_pass_correct or is_admin_pass_correct:
        session['admin_verified'] = True
        return redirect(url_for('gestione_utenti'))
    else:
        flash("Password di sicurezza errata. Inserisci la tua password utente.", "danger")
        return redirect(url_for('conferma_admin'))

@app.route('/gestione_utenti')
@admin_required
def gestione_utenti():
    if not session.get('admin_verified'):
        return redirect(url_for('conferma_admin'))

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, username, permissions FROM users ORDER BY username ASC")
    raw_users = cur.fetchall()
    cur.close()
    conn.close()
    # Normalizza permissions NULL -> dict con valori di default
    users = []
    for u in raw_users:
        u_dict = dict(u)
        if u_dict.get('permissions') is None:
            u_dict['permissions'] = {"can_edit": True, "can_delete": True, "can_manage_users": False, "can_view_anagrafica": True, "is_schiavoni": True}
        users.append(u_dict)
    return render_template('gestione_utenti.html', users=users)

@app.route('/aggiungi_utente', methods=['POST'])
@admin_required
def aggiungi_utente():
    if not session.get('admin_verified'): return redirect(url_for('conferma_admin'))
    username = request.form.get('username')
    password = request.form.get('password')
    is_schiavoni = request.form.get('is_schiavoni') == 'on'
    if not username or not password:
        flash("Username e password sono obbligatori.", 'warning')
        return redirect(url_for('gestione_utenti'))
    
    password_hash = generate_password_hash(password)
    import json
    default_perms = json.dumps({
        "can_edit": True,
        "can_delete": True,
        "can_manage_users": False,
        "can_view_anagrafica": True,
        "is_schiavoni": is_schiavoni
    })
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO users (username, password_hash, permissions) VALUES (%s, %s, %s)", (username, password_hash, default_perms))
        conn.commit()
        flash(f"Utente '{username}' creato con successo!", 'success')
    except psycopg2.IntegrityError:
        flash(f"L'utente '{username}' esiste già.", 'danger')
        conn.rollback()
    except Exception as e:
        flash(f"Errore durante la creazione dell'utente: {e}", 'danger')
        conn.rollback()
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('gestione_utenti'))

@app.route('/aggiorna_permessi_utente/<int:id>', methods=['POST'])
@admin_required
def aggiorna_permessi_utente(id):
    if not session.get('admin_verified'): return redirect(url_for('conferma_admin'))
    
    can_edit = request.form.get('can_edit') == 'on'
    can_delete = request.form.get('can_delete') == 'on'
    can_manage_users = request.form.get('can_manage_users') == 'on'
    can_view_anagrafica = request.form.get('can_view_anagrafica') == 'on'
    is_schiavoni = request.form.get('is_schiavoni') == 'on'
    
    import json
    perms = json.dumps({
        "can_edit": can_edit,
        "can_delete": can_delete,
        "can_manage_users": can_manage_users,
        "can_view_anagrafica": can_view_anagrafica,
        "is_schiavoni": is_schiavoni
    })
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET permissions = %s WHERE id = %s", (perms, id))
        conn.commit()
        if session.get('user_id') == id:
            session['permissions'] = json.loads(perms)
        flash("Permessi aggiornati con successo.", "success")
    except Exception as e:
        flash(f"Errore aggiornamento permessi: {e}", "danger")
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('gestione_utenti'))

@app.route('/modifica_password', methods=['POST'])
@admin_required
def modifica_password():
    if not session.get('admin_verified'): return redirect(url_for('conferma_admin'))
    user_id = request.form.get('user_id')
    new_password = request.form.get('new_password')
    if not user_id or not new_password:
        flash("ID utente e nuova password sono obbligatori.", 'warning')
        return redirect(url_for('gestione_utenti'))
    
    password_hash = generate_password_hash(new_password)
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id))
        conn.commit()
        flash("Password aggiornata con successo.", 'success')
    except Exception as e:
        flash(f"Errore durante l'aggiornamento della password: {e}", 'danger')
        conn.rollback()
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('gestione_utenti'))

@app.route('/elimina_utente/<int:id>', methods=['POST'])
@admin_required
def elimina_utente(id):
    if not session.get('admin_verified'): return redirect(url_for('conferma_admin'))
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as count FROM users")
        if cur.fetchone()['count'] <= 1:
            flash("Impossibile eliminare l'ultimo utente.", 'danger')
            return redirect(url_for('gestione_utenti'))

        cur.execute("DELETE FROM users WHERE id = %s", (id,))
        conn.commit()
        flash("Utente eliminato con successo.", 'success')
        # Se l'utente elimina se stesso, fa il logout
        if session.get('user_id') == id:
            return redirect(url_for('logout'))
    except Exception as e:
        flash(f"Errore durante l'eliminazione dell'utente: {e}", 'danger')
        conn.rollback()
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('gestione_utenti'))

# --- PARSING E FILTRI ---
def _parse_salato_item(item_string, default_unit=None, default_qta=None):
    """Estrae quantità+unità e nome prodotto da una riga di testo salato.
    Riconosce unità come kg, gr, pezzi, persone, vassoi, ecc.
    Se non trova nulla nel testo usa default_unit/default_qta come fallback.
    """
    if not item_string:
        item_string = ""
    item_string = item_string.strip()

    def _fmt_num(v):
        try:
            f = float(str(v).replace(',', '.'))
            return str(int(f)) if f == int(f) else f'{f:g}'
        except Exception:
            return str(v)

    units_pattern = (
        r'(kg|chili|chilo|grammi|grammo|gr\.?|g(?=\s|$)|'
        r'persone|persona|pers\.?|pezzi|pezzo|pz\.?|pzz|'
        r'vassoi|vassoio|teglie|teglia|porzioni|porzione)'
    )

    quantity_str = ""
    name_str = item_string

    # Pattern 1: numero + unità (es: "40 pezzi", "1.5 kg", "per 20 persone")
    m = re.search(
        r'(?:(?:per|x|da)\s+)?(\d+(?:[.,]\d+)?)\s*' + units_pattern + r'\b',
        item_string, flags=re.IGNORECASE
    )
    if m:
        quantity_str = f"{_fmt_num(m.group(1))} {m.group(2).lower()}"
        name_str = (item_string[:m.start()] + ' ' + item_string[m.end():]).strip()
    else:
        # Pattern 2: unità + numero (es: "kg 1.5", "pz: 30")
        m2 = re.search(
            r'\b' + units_pattern + r'\s*[:=]?\s*(\d+(?:[.,]\d+)?)',
            item_string, flags=re.IGNORECASE
        )
        if m2:
            quantity_str = f"{_fmt_num(m2.group(2))} {m2.group(1).lower()}"
            name_str = (item_string[:m2.start()] + ' ' + item_string[m2.end():]).strip()
        else:
            # Pattern 3: solo numero, usa unità default se disponibile
            m3 = re.search(r'(\d+(?:[.,]\d+)?)', item_string)
            if m3:
                unit = (default_unit or '').strip().lower()
                quantity_str = f"{_fmt_num(m3.group(1))} {unit}".strip()
                name_str = (item_string[:m3.start()] + ' ' + item_string[m3.end():]).strip()
            elif default_qta:
                q = _fmt_num(default_qta)
                unit = (default_unit or '').strip().lower()
                quantity_str = f"{q} {unit}".strip() if q else ''

    # Pulizia nome
    name_str = re.sub(r'\b(?:per|x|da)\b', ' ', name_str, flags=re.IGNORECASE)
    name_str = re.sub(r'[\(\)\[\]\:\,]', ' ', name_str)
    name_str = re.sub(r'\s+', ' ', name_str).strip()
    if not name_str:
        name_str = item_string.strip()

    return quantity_str, name_str.upper()

def _parse_rinfresco(descrizione_completa):
    data = {'torte': [], 'salato': [], 'altro': []}
    if not descrizione_completa: return data
    try:
        parts = re.split(r'(LABORATORIO:|SALATO:|ALTRO:)', descrizione_completa, flags=re.IGNORECASE)
        current_section = None
        for part in parts:
            part_upper = part.strip().upper()
            if part_upper in ['LABORATORIO:', 'SALATO:', 'ALTRO:']:
                current_section = part_upper.replace(':', '').lower()
            elif current_section and part.strip():
                items = [item.strip() for item in part.strip().split('|') if item.strip()]
                if current_section == 'laboratorio':
                    for item in items:
                        torta_data = {'tipo': '', 'qta': '', 'unita': '', 'gusto': '', 'note': ''}
                        qta_match = re.search(r'^(\d+[.,]?\d*)\s*([a-zA-Z]+)', item)
                        if qta_match:
                            torta_data['qta'] = qta_match.group(1).replace(',', '.')
                            torta_data['unita'] = qta_match.group(2)
                            item_rest = item[qta_match.end():].strip()
                        else:
                            item_rest = item
                        note_match = re.search(r'NOTE:\s*(.*)', item_rest, re.IGNORECASE | re.DOTALL)
                        if note_match:
                            torta_data['note'] = note_match.group(1).strip()
                            item_rest = item_rest[:note_match.start()].strip()
                        gusto_match = re.search(r'GUSTO:\s*(.*)', item_rest, re.IGNORECASE | re.DOTALL)
                        if gusto_match:
                            torta_data['gusto'] = gusto_match.group(1).strip()
                            item_rest = item_rest[:gusto_match.start()].strip()
                        torta_data['tipo'] = item_rest.strip().upper()
                        data['torte'].append(torta_data)
                elif current_section == 'salato': data['salato'].extend(items)
                elif current_section == 'altro': data['altro'].extend(items)
    except Exception: pass
    return data

@app.template_filter('formato_data')
def formato_data(value):
    if not value: return ""
    try:
        if hasattr(value, 'strftime'): return value.strftime('%d/%m/%Y')
        dt = datetime.strptime(str(value), '%Y-%m-%d')
        return dt.strftime('%d/%m/%Y')
    except: return str(value)

@app.template_filter('formato_ora')
def formato_ora(value):
    return value.strftime('%H:%M') if isinstance(value, time) else '--:--'

@app.template_filter('formato_timestamp')
def formato_timestamp(dt):
    if not dt: return ""
    if isinstance(dt, str): return dt
    try:
        if ROME_TZ and hasattr(dt, 'astimezone'):
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(ROME_TZ).strftime('%d/%m/%Y %H:%M')
        return dt.strftime('%d/%m/%Y %H:%M')
    except Exception:
        try:
            return dt.strftime('%d/%m/%Y %H:%M')
        except Exception:
            return str(dt)

@app.template_filter('formato_nota_data')
def formato_nota_data(dt):
    if not dt: return ""
    try:
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
        if ROME_TZ and hasattr(dt, 'astimezone'):
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(ROME_TZ).strftime('%d/%m/%Y alle %H:%M')
        elif getattr(dt, 'tzinfo', None) is not None:
            return dt.astimezone(timezone(timedelta(hours=2))).strftime('%d/%m/%Y alle %H:%M')
        return dt.strftime('%d/%m/%Y alle %H:%M')
    except Exception:
        try:
            return dt.strftime('%d/%m/%Y alle %H:%M')
        except Exception:
            return str(dt)

# --- ROTTE PRINCIPALI DELL'APPLICAZIONE ---

@app.route('/')
def index():
    # La pagina principale ora è la produzione
    return redirect(url_for('produzione'))

@app.route('/nuovo_ordine')
@permission_required('can_edit')
def nuovo_ordine_form():
    # La vecchia index page ora è qui
    return render_template('nuovo_ordine.html', oggi=now_italy().date())

@app.route('/produzione')
def produzione():
    now_curr = now_italy()
    mese = int(request.args.get('mese', now_curr.month))
    anno = int(request.args.get('anno', now_curr.year))
    prev_mese, prev_anno = (mese - 1, anno) if mese > 1 else (12, anno - 1)
    next_mese, next_anno = (mese + 1, anno) if mese < 12 else (1, anno + 1)

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ordini WHERE EXTRACT(MONTH FROM data_consegna) = %s AND EXTRACT(YEAR FROM data_consegna) = %s", (mese, anno))
    ordini = cur.fetchall()
    cur.close()
    conn.close()
    
    cal = calendar.Calendar(firstweekday=0)
    tabellone = [[{'data': g, 'fuori_mese': g.month != mese, 'ordini': [o for o in ordini if o['data_consegna'] == g]} for g in sett] for sett in cal.monthdatescalendar(anno, mese)]
    mesi = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    
    return render_template('produzione.html', tabellone=tabellone, mese_nome=mesi[mese-1], mese=mese, anno=anno, prev_mese=prev_mese, prev_anno=prev_anno, next_mese=next_mese, next_anno=next_anno, oggi=now_curr.date(), conteggio_ordini=len(ordini))

@app.route('/giorno/<data>')
def gestione_giorno(data):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ordini WHERE data_consegna = %s ORDER BY orario ASC", (data,))
    ordini_raw = cur.fetchall()
    ordini = [dict(o, rinfresco_items=_parse_rinfresco(o.get('descrizione', ''))) if o['categoria'] == 'Rinfresco' else o for o in ordini_raw]
    cur.close()
    conn.close()
    return render_template('gestione_giorno.html', ordini=ordini, data_it=data, data_raw=data)

@app.route('/api/ordine/set_stampato/<int:id>', methods=['POST'])
def set_stampato(id):
    try:
        import json as _json
        username = session.get('username', 'Sconosciuto')
        now = now_italy()
        formatted = now.strftime('%d/%m/%Y %H:%M')

        conn = get_db_connection()
        cur = conn.cursor()

        # Leggi il log corrente dal DB
        cur.execute("SELECT print_log FROM ordini WHERE id = %s", (id,))
        row = cur.fetchone()
        current_log = list(row['print_log']) if row and row['print_log'] else []

        # Cerca se questo utente è già nel log
        trovato = False
        for voce in current_log:
            if voce.get('utente') == username:
                voce['data'] = formatted  # aggiorna timestamp
                trovato = True
                break

        # Se è un utente nuovo, aggiunge la voce
        if not trovato:
            current_log.append({'utente': username, 'data': formatted})

        cur.execute("""
            UPDATE ordini 
            SET stampato = TRUE,
                stampato_da = %s,
                stampato_il = %s,
                print_log = %s::jsonb
            WHERE id = %s
        """, (username, now, _json.dumps(current_log), id))
        conn.commit()
        return jsonify({'success': True, 'utente': username, 'data': formatted, 'is_new': not trovato})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cur.close()
        conn.close()

@app.route('/anteprima_ordine/<int:id>')
def anteprima_ordine(id):
    """Visualizza l'ordine come verrebbe stampato, senza avviare la stampa automatica."""
    torta_id = request.args.get('torta_id', type=int, default=None)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ordini WHERE id = %s", (id,))
    ordine = cur.fetchone()
    cur.close()
    conn.close()

    if not ordine: return "Ordine non trovato", 404

    o = dict(ordine)
    descrizione_completa = o.get('descrizione', '')
    categoria = o.get('categoria', '')

    dettagli_rinfresco = {'torte': [], 'salato': [], 'altro': []}
    note_torta_generica = ""
    farcitura_generica = o.get('farcitura', '').strip()

    if categoria == 'Rinfresco':
        dettagli_rinfresco = _parse_rinfresco(descrizione_completa)
        if torta_id is not None and 0 <= torta_id < len(dettagli_rinfresco['torte']):
            torta_specifica = dettagli_rinfresco['torte'][torta_id]
            o['tipo_dolce'] = torta_specifica.get('tipo')
            o['kg'] = torta_specifica.get('qta')
            o['unita_misura'] = torta_specifica.get('unita')
            note_torta_generica = torta_specifica.get('note')
            farcitura_generica = torta_specifica.get('gusto')
            dettagli_rinfresco = {'torte': [], 'salato': [], 'altro': []}
    else:
        note_torta_generica = descrizione_completa

    return render_template(
        'stampa_singola.html',
        o=o,
        rinfresco=dettagli_rinfresco,
        note_torta=note_torta_generica,
        farcitura=farcitura_generica,
        torta_id=torta_id,
        anteprima=True
    )

# ... (il resto delle rotte rimane invariato)
@app.route('/salato_giorno/<data>')
def salato_giorno(data):
    return redirect(url_for('nota_salato', data=data))

@app.route('/consegne_giorno/<data>')
def consegne_giorno(data):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ordini WHERE data_consegna = %s AND modalita = 'Consegna' ORDER BY orario ASC", (data,))
    ordini = cur.fetchall()
    cur.close()
    conn.close()
    now = now_italy().strftime('%d/%m/%Y %H:%M')
    return render_template('stampa_consegne.html', ordini=ordini, data_it=data, now=now)

def _estrai_voci_salato(ordine):
    desc = ordine.get('descrizione', '') or ''
    cat = (ordine.get('categoria', '') or '').lower()
    tipo_dolce = (ordine.get('tipo_dolce', '') or '').strip()

    raw_voci = []

    # 1. Sezione SALATO: nella descrizione (es. Rinfreschi)
    if 'salato:' in desc.lower():
        m = re.search(r'\bSALATO:\s*(.*?)(?=\s*(?:\|\s*)?(?:LABORATORIO:|ALTRO:|$))', desc, re.IGNORECASE | re.DOTALL)
        if m and m.group(1).strip():
            salato_str = m.group(1).strip()
            # Dividi per pipe, newline o punto e virgola
            blocks = re.split(r'[|\n;]+', salato_str)
            for b in blocks:
                b = b.strip()
                if not b: continue
                # Se nel blocco ci sono virgole che separano elementi (es. 40 pizzette rosse, 30 bottoncini assortiti)
                subs = [s.strip() for s in b.split(',') if s.strip()]
                if len(subs) > 1:
                    raw_voci.extend(subs)
                else:
                    raw_voci.append(b)

    # 2. Categoria Salato
    if cat == 'salato':
        if tipo_dolce and tipo_dolce.upper() != 'RINFRESCO COMPLETO':
            # Separa per virgola, punto e virgola, newline o ' e ' / ' + '
            parts = re.split(r'[,;\n|]|\s+(?:e|\+)\s+', tipo_dolce, flags=re.IGNORECASE)
            for p in parts:
                p = p.strip()
                if p and p.upper() not in [v.upper() for v in raw_voci]:
                    raw_voci.append(p)
        if desc and 'salato:' not in desc.lower():
            dp_list = re.split(r'[,;\n|]+', desc)
            for dp in dp_list:
                dp = dp.strip()
                if dp and dp.upper() not in [v.upper() for v in raw_voci]:
                    raw_voci.append(dp)

    return raw_voci

@app.route('/stampa_vassoi_salato/<int:id>')
def stampa_vassoi_salato(id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ordini WHERE id = %s", (id,))
    ordine = cur.fetchone()
    cur.close()
    conn.close()
    if not ordine: return "Ordine non trovato", 404

    def_unit = (ordine.get('unita_misura') or '').strip()
    def_qta  = ordine.get('kg') or None

    raw_voci_salato = _estrai_voci_salato(dict(ordine))

    voci_da_stampare = []

    if len(raw_voci_salato) > 1:
        # Due o più prodotti trovati: crea etichetta per ciascun prodotto
        for voce in raw_voci_salato:
            voce = voce.strip()
            if not voce: continue
            qta, nome_prod = _parse_salato_item(voce, default_unit=def_unit, default_qta=None)
            if not qta and def_qta and def_unit.lower() in ['pezzi', 'pz']:
                qta = '1 pezzo'
            elif not qta and def_unit and def_qta:
                qta = f"{def_qta} {def_unit}"
            voci_da_stampare.append({
                'orario': ordine.get('orario'),
                'cliente': ordine.get('cliente'),
                'negozio': ordine.get('negozio'),
                'tipo_dolce': nome_prod,
                'kg': qta,
                'unita_misura': ''
            })
    elif len(raw_voci_salato) == 1:
        voce = raw_voci_salato[0].strip()
        qta, nome_prod = _parse_salato_item(voce, default_unit=def_unit, default_qta=def_qta)

        # Se la quantità indica 2 o più pezzi/vassoi/teglie (es. '2 pezzi', '3 pezzi', '2 vassoi')
        # generiamo etichette separate per ciascun pezzo/prodotto
        m_qta = re.search(r'^(\d+)\s*(pezzi|pezzo|pz|pzz|vassoi|vassoio|teglie|teglia)\b', qta, re.IGNORECASE)
        num_pezzi = int(m_qta.group(1)) if m_qta else 0

        if 2 <= num_pezzi <= 20:
            unit_name = m_qta.group(2).lower()
            single_unit = 'pezzo' if ('pez' in unit_name or 'pz' in unit_name) else ('vassoio' if 'vasso' in unit_name else 'teglia')
            for i in range(1, num_pezzi + 1):
                voci_da_stampare.append({
                    'orario': ordine.get('orario'),
                    'cliente': ordine.get('cliente'),
                    'negozio': ordine.get('negozio'),
                    'tipo_dolce': nome_prod,
                    'kg': f'1 {single_unit} ({i} di {num_pezzi})',
                    'unita_misura': ''
                })
        else:
            voci_da_stampare.append({
                'orario': ordine.get('orario'),
                'cliente': ordine.get('cliente'),
                'negozio': ordine.get('negozio'),
                'tipo_dolce': nome_prod,
                'kg': qta,
                'unita_misura': ''
            })
    else:
        # Fallback se non ci sono voci estratte ma c'è tipo_dolce
        td = ordine.get('tipo_dolce') or ''
        if td:
            qta, nome_prod = _parse_salato_item(td, default_unit=def_unit, default_qta=def_qta)
            voci_da_stampare.append({
                'orario': ordine.get('orario'),
                'cliente': ordine.get('cliente'),
                'negozio': ordine.get('negozio'),
                'tipo_dolce': nome_prod,
                'kg': qta,
                'unita_misura': ''
            })

    return render_template(
        'stampa_salato_multiplo.html',
        vassoi=voci_da_stampare,
        o=dict(ordine),
        data_consegna=ordine.get('data_consegna')
    )

@app.route('/salato/<data>')
def nota_salato(data):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM ordini WHERE data_consegna = %s ORDER BY orario ASC", (data,))
        ordini_raw = cur.fetchall()
        cur.close()
        conn.close()
        
        prodotti_salato = []
        for o in ordini_raw:
            ord_dict = dict(o)
            def_unit = (ord_dict.get('unita_misura') or '').strip()
            def_qta  = ord_dict.get('kg') or None

            voci_salato = _estrai_voci_salato(ord_dict)

            for voce in voci_salato:
                voce = voce.strip()
                if voce:
                    qta, nome_prod = _parse_salato_item(voce, default_unit=def_unit, default_qta=def_qta)
                    if nome_prod:
                        prodotti_salato.append({
                            'orario': ord_dict['orario'],
                            'cliente': ord_dict['cliente'],
                            'negozio': ord_dict['negozio'],
                            'tipo_dolce': nome_prod,
                            'kg': qta
                        })
                        
        # Converti data da YYYY-MM-DD a GG/MM/AAAA
        try:
            from datetime import datetime as _dt
            data_formattata = _dt.strptime(data, '%Y-%m-%d').strftime('%d/%m/%Y')
        except Exception:
            data_formattata = data
        return render_template('stampa_salato.html', ordini=prodotti_salato, data_it=data_formattata, data_raw=data)
        
    except Exception as e:
        return f"Errore Tecnico: {str(e)}", 500


@app.route('/laboratorio/<data>')
def laboratorio_giorno(data):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM ordini 
        WHERE data_consegna = %s AND 
              (LOWER(categoria) = 'torta' OR categoria = 'Rinfresco')
        ORDER BY orario ASC
    """, (data,))
    ordini_raw = cur.fetchall()
    cur.close()
    conn.close()

    items_per_laboratorio = []
    for o_dict in ordini_raw:
        categoria = o_dict.get('categoria', '').lower()

        if categoria == 'rinfresco':
            dati_rinfresco = _parse_rinfresco(o_dict.get('descrizione', ''))
            torte = dati_rinfresco.get('torte', [])
            
            if not torte and o_dict.get('tipo_dolce') and o_dict.get('tipo_dolce') != 'RINFRESCO COMPLETO':
                items_per_laboratorio.append(o_dict)
                continue

            for torta in torte:
                item = dict(o_dict) 
                item['tipo_dolce'] = torta.get('tipo')
                item['kg'] = torta.get('qta') if torta.get('qta') else 0
                item['unita_misura'] = torta.get('unita')
                item['farcitura'] = torta.get('gusto')
                item['descrizione'] = torta.get('note')
                item['is_from_rinfresco'] = True 
                items_per_laboratorio.append(item)
        else: 
            items_per_laboratorio.append(o_dict)

    now = now_italy().strftime('%d/%m/%Y %H:%M')
    return render_template('laboratorio.html', ordini=items_per_laboratorio, data_it=data, now=now)


@app.route('/nota_giornaliera/<data>')
def nota_giornaliera(data):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ordini WHERE data_consegna = %s ORDER BY orario ASC, id ASC", (data,))
    ordini = cur.fetchall()
    cur.close()
    conn.close()
    now = now_italy().strftime('%d/%m/%Y %H:%M')
    return render_template('nota_giornaliera.html', ordini=ordini, data_it=data, now=now)


@app.route('/lista_totale/<data>')
def lista_totale(data):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ordini WHERE data_consegna = %s AND (negozio = 'Palocco' OR negozio = 'Secondo negozio') ORDER BY orario ASC", (data,))
    ordini = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('stampa_giorno.html', ordini=ordini, data_it=data)

@app.route('/stampa_ordine/<int:id>')
def stampa_ordine(id):
    torta_id = request.args.get('torta_id', type=int, default=None)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ordini WHERE id = %s", (id,))
    ordine = cur.fetchone()
    cur.close()
    conn.close()
    
    if not ordine: return "Ordine non trovato", 404
    
    o = dict(ordine)
    descrizione_completa = o.get('descrizione', '')
    categoria = o.get('categoria', '')

    dettagli_rinfresco = {'torte': [], 'salato': [], 'altro': []}
    note_torta_generica = ""
    farcitura_generica = o.get('farcitura', '').strip()

    if categoria == 'Rinfresco':
        dettagli_rinfresco = _parse_rinfresco(descrizione_completa)
        if torta_id is not None and 0 <= torta_id < len(dettagli_rinfresco['torte']):
            torta_specifica = dettagli_rinfresco['torte'][torta_id]
            o['tipo_dolce'] = torta_specifica.get('tipo')
            o['kg'] = torta_specifica.get('qta')
            o['unita_misura'] = torta_specifica.get('unita')
            note_torta_generica = torta_specifica.get('note')
            farcitura_generica = torta_specifica.get('gusto')
            dettagli_rinfresco = {'torte': [], 'salato': [], 'altro': []}
    else:
        note_torta_generica = descrizione_completa

    return render_template(
        'stampa_singola.html', 
        o=o, 
        rinfresco=dettagli_rinfresco,
        note_torta=note_torta_generica,
        farcitura=farcitura_generica,
        torta_id=torta_id,
        anteprima=False
    )

@app.route('/stampa_singola_debug/<int:id>')
def stampa_singola_debug(id):
    torta_id = request.args.get('torta_id', type=int, default=None)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ordini WHERE id = %s", (id,))
    ordine = cur.fetchone()
    cur.close()
    conn.close()
    
    if not ordine: return "Ordine non trovato", 404
    
    o = dict(ordine)
    descrizione_completa = o.get('descrizione', '')
    categoria = o.get('categoria', '')

    dettagli_rinfresco = {'torte': [], 'salato': [], 'altro': []}
    note_torta_generica = ""
    farcitura_generica = o.get('farcitura', '').strip()

    if categoria == 'Rinfresco':
        dettagli_rinfresco = _parse_rinfresco(descrizione_completa)
        if torta_id is not None and 0 <= torta_id < len(dettagli_rinfresco['torte']):
            torta_specifica = dettagli_rinfresco['torte'][torta_id]
            o['tipo_dolce'] = torta_specifica.get('tipo')
            o['kg'] = torta_specifica.get('qta')
            o['unita_misura'] = torta_specifica.get('unita')
            note_torta_generica = torta_specifica.get('note')
            farcitura_generica = torta_specifica.get('gusto')
            dettagli_rinfresco = {'torte': [], 'salato': [], 'altro': []}
    else:
        note_torta_generica = descrizione_completa

    return render_template(
        'stampa_singola.html', 
        o=o, 
        rinfresco=dettagli_rinfresco,
        note_torta=note_torta_generica,
        farcitura=farcitura_generica,
        torta_id=torta_id,
        anteprima=False
    )

@app.route('/stampa_torta_rinfresco/<int:id>')
def stampa_torta_rinfresco(id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ordini WHERE id = %s", (id,))
    ordine = cur.fetchone()
    cur.close()
    conn.close()
    
    if not ordine: return "Ordine non trovato", 404
    
    o = dict(ordine)
    dati_rinfresco = _parse_rinfresco(o.get('descrizione', ''))
    torte = dati_rinfresco.get('torte', [])

    if not torte and o.get('tipo_dolce') != 'RINFRESCO COMPLETO':
        torte.append({
            'tipo': o.get('tipo_dolce'),
            'qta': o.get('kg'),
            'unita': o.get('unita_misura'),
            'gusto': o.get('farcitura'),
            'note': '' 
        })

    return render_template('stampa_torte_rinfresco.html', o=o, torte=torte)

@app.route('/modifica_ordine_form/<int:id>', methods=['GET', 'POST'])
@permission_required('can_edit')
def modifica_ordine_form(id):
    conn = get_db_connection()
    cur = conn.cursor()
    if request.method == 'POST':
        d = request.form
        orario = d.get('orario')
        if not orario or orario.isspace(): orario = None

        cur.execute("SELECT foto, foto_descrizione FROM ordini WHERE id = %s", (id,))
        old_ord = cur.fetchone() or {}

        nuova_foto = process_uploaded_image(request.files.get('foto'))
        if d.get('rimuovi_foto') == '1':
            foto_finale = None
        elif nuova_foto:
            foto_finale = nuova_foto
        else:
            foto_finale = old_ord.get('foto')

        foto_desc = d.get('foto_descrizione', '')

        query = """
            UPDATE ordini 
            SET cliente=%s, telefono=%s, negozio=%s, tipo_dolce=%s, farcitura=%s, 
                kg=%s, unita_misura=%s, orario=%s, descrizione=%s, categoria=%s, 
                pagamento_tipo=%s, pagamento_importo=%s, data_consegna=%s, modalita=%s, indirizzo=%s, canale=%s,
                foto=%s, foto_descrizione=%s
            WHERE id=%s
        """
        params = (
            d.get('cliente'), d.get('telefono'), d.get('negozio'), d.get('tipo_dolce'), 
            d.get('farcitura'), d.get('kg') or 0, d.get('unita_misura'), orario, 
            d.get('descrizione'), d.get('categoria'), d.get('pagamento_tipo'), 
            d.get('pagamento_importo') or 0, d.get('data_consegna'), d.get('modalita'), d.get('indirizzo'), d.get('canale'),
            foto_finale, foto_desc, id
        )
        cur.execute(query, params)
        conn.commit()
        cur.close()
        conn.close()
        return redirect(url_for('gestione_giorno', data=d.get('data_consegna')))
    
    cur.execute("SELECT * FROM ordini WHERE id = %s", (id,))
    ordine = cur.fetchone()
    cur.close()
    conn.close()
    return render_template('modifica.html', o=ordine)

@app.route('/nuovo_rinfresco')
@permission_required('can_edit')
def nuovo_rinfresco():
    oggi = now_italy().strftime('%Y-%m-%d')
    return render_template('nuovo_rinfresco.html', oggi=oggi)

@app.route('/salva_rinfresco_unico', methods=['POST'])
@permission_required('can_edit')
def salva_rinfresco_unico():
    try:
        d = request.form
        
        tipi = d.getlist('torta_tipo[]')
        quantita = d.getlist('torta_quantita[]')
        unita = d.getlist('torta_unita[]')
        gusti = d.getlist('torta_gusto[]') 
        note_torte = d.getlist('torta_note[]')

        desc_parts = []
        torte_strings = []

        for i in range(len(tipi)):
            tipo = tipi[i].strip().upper()
            qta = quantita[i].strip()
            uni = unita[i].strip()
            gusto = gusti[i].strip()
            note = note_torte[i].strip()

            if tipo:
                torta_str = f"{qta} {uni} {tipo}"
                if gusto: torta_str += f" GUSTO: {gusto}"
                if note: torta_str += f" NOTE: {note}"
                torte_strings.append(torta_str)

        if torte_strings:
            desc_parts.append(f"LABORATORIO: {' | '.join(torte_strings)}")

        salato_items = [s.strip() for s in d.getlist('salato_testo[]') if s.strip()]
        if salato_items:
            desc_parts.append(f"SALATO: {' | '.join(salato_items)}")
        
        altro_items = [a.strip() for a in d.getlist('altro_testo[]') if a.strip()]
        if altro_items:
            desc_parts.append(f"ALTRO: {' | '.join(altro_items)}")

        descrizione_finale = ' | '.join(desc_parts)
        orario = d.get('orario')
        if not orario or orario.isspace(): orario = None

        foto_b64 = process_uploaded_image(request.files.get('foto'))
        foto_desc = d.get('foto_descrizione', '')

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO ordini (cliente, telefono, negozio, data_consegna, orario, operatore, modalita, indirizzo, 
                              canale, categoria, tipo_dolce, kg, unita_misura, farcitura, descrizione, 
                              pagamento_tipo, pagamento_importo, foto, foto_descrizione)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'Rinfresco', %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            d.get('cliente'), d.get('telefono'), d.get('negozio'), d.get('data_consegna'), orario,
            d.get('operatore'), d.get('modalita'), d.get('indirizzo'), d.get('canale'),
            'RINFRESCO COMPLETO', 
            0,
            '',
            '',
            descrizione_finale, 
            d.get('pagamento_tipo'), d.get('pagamento_importo') or 0,
            foto_b64, foto_desc
        ))
        save_or_update_client(cur, d.get('cliente'), d.get('telefono'))
        conn.commit()
        cur.close()
        conn.close()
        return redirect(url_for('gestione_giorno', data=d.get('data_consegna')))
    except Exception as e: 
        return f"Errore salvataggio: {e}", 500

@app.route('/modifica_rinfresco/<int:id>', methods=['GET', 'POST'])
@permission_required('can_edit')
def modifica_rinfresco(id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM ordini WHERE id = %s", (id,))
        o_row = cur.fetchone()
        cur.close()
        conn.close()
        if not o_row:
            flash("Ordine non trovato.", "danger")
            return redirect(url_for('produzione'))
        
        o = dict(o_row)
        desc = o.get('descrizione') or ''
        dati_rinfresco = _parse_rinfresco(desc)
        if not dati_rinfresco.get('torte') and not dati_rinfresco.get('salato') and not dati_rinfresco.get('altro'):
            tipo_dolce = (o.get('tipo_dolce') or '').strip()
            cat = (o.get('categoria') or '').strip().lower()
            if tipo_dolce and tipo_dolce.upper() != 'RINFRESCO COMPLETO':
                qta_str = ''
                try:
                    kg_raw = str(o.get('kg') or '').replace(',', '.').strip()
                    if kg_raw:
                        v = float(kg_raw)
                        if v > 0:
                            qta_str = f"{v:g}"
                except Exception:
                    qta_str = str(o.get('kg') or '').strip()
                    if qta_str == '0':
                        qta_str = ''
                
                dati_rinfresco['torte'].append({
                    'tipo': tipo_dolce,
                    'qta': qta_str,
                    'unita': o.get('unita_misura') or 'Kg',
                    'gusto': o.get('farcitura') or '',
                    'note': desc
                })
            elif cat == 'salato' and desc:
                dati_rinfresco['salato'].append(desc)
            elif desc:
                dati_rinfresco['altro'].append(desc)
        
        return render_template('modifica_rinfresco.html', o=o, rinfresco=dati_rinfresco)
    except Exception as e:
        print(f"Errore modifica_rinfresco: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Errore caricamento rinfresco: {e}", "danger")
        return redirect(url_for('modifica_ordine_form', id=id))

@app.route('/salva_modifica_rinfresco/<int:id>', methods=['POST'])
@permission_required('can_edit')
def salva_modifica_rinfresco(id):
    try:
        d = request.form
        
        tipi = d.getlist('torta_tipo[]')
        quantita = d.getlist('torta_quantita[]')
        unita = d.getlist('torta_unita[]')
        gusti = d.getlist('torta_gusto[]') 
        note_torte = d.getlist('torta_note[]')

        desc_parts = []
        torte_strings = []

        for i in range(len(tipi)):
            tipo = tipi[i].strip().upper()
            qta = quantita[i].strip() if i < len(quantita) else ''
            uni = unita[i].strip() if i < len(unita) else ''
            gusto = gusti[i].strip() if i < len(gusti) else ''
            note = note_torte[i].strip() if i < len(note_torte) else ''

            if tipo:
                torta_str = f"{qta} {uni} {tipo}".strip()
                if gusto: torta_str += f" GUSTO: {gusto}"
                if note: torta_str += f" NOTE: {note}"
                torte_strings.append(torta_str)

        if torte_strings:
            desc_parts.append(f"LABORATORIO: {' | '.join(torte_strings)}")

        salato_items = [s.strip() for s in d.getlist('salato_testo[]') if s.strip()]
        if salato_items:
            desc_parts.append(f"SALATO: {' | '.join(salato_items)}")
        
        altro_items = [a.strip() for a in d.getlist('altro_testo[]') if a.strip()]
        if altro_items:
            desc_parts.append(f"ALTRO: {' | '.join(altro_items)}")

        descrizione_nuova = ' | '.join(desc_parts)
        
        importo_raw = (d.get('pagamento_importo') or '0').replace(',', '.').strip()
        try:
            importo = float(importo_raw) if importo_raw else 0.0
        except Exception:
            importo = 0.0
        
        orario = d.get('orario')
        if not orario or orario.isspace(): orario = None

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT foto, foto_descrizione FROM ordini WHERE id = %s", (id,))
        old_ord = cur.fetchone() or {}

        nuova_foto = process_uploaded_image(request.files.get('foto'))
        if d.get('rimuovi_foto') == '1':
            foto_finale = None
        elif nuova_foto:
            foto_finale = nuova_foto
        else:
            foto_finale = old_ord.get('foto')

        foto_desc = d.get('foto_descrizione', '')

        indirizzo_finale = d.get('indirizzo') or d.get('indirizzo_consegna') or ''

        cur.execute("""
            UPDATE ordini 
            SET cliente=%s, telefono=%s, negozio=%s, data_consegna=%s, orario=%s, 
                operatore=%s, modalita=%s, indirizzo=%s, categoria='Rinfresco', tipo_dolce=%s, kg=%s, unita_misura=%s, farcitura=%s,
                descrizione=%s, pagamento_tipo=%s, pagamento_importo=%s, canale=%s,
                foto=%s, foto_descrizione=%s
            WHERE id=%s
        """, (
            d.get('cliente'), d.get('telefono'), d.get('negozio'), d.get('data_consegna'), 
            orario, d.get('operatore'), d.get('modalita'), indirizzo_finale,
            'RINFRESCO COMPLETO', 0, '', '',
            descrizione_nuova, d.get('pagamento_tipo'), importo, d.get('canale'),
            foto_finale, foto_desc, id
        ))
        save_or_update_client(cur, d.get('cliente'), d.get('telefono'))
        conn.commit()
        cur.close()
        conn.close()
        
        return redirect(url_for('gestione_giorno', data=d.get('data_consegna')))
    except Exception as e:
        return f"Errore: {str(e)}", 500
    
@app.route('/elimina_ordine/<int:id>')
@permission_required('can_delete')
def elimina_ordine(id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT data_consegna FROM ordini WHERE id = %s", (id,))
    res = cur.fetchone()
    data_ritorno = str(res['data_consegna']) if res else ""
    cur.execute("DELETE FROM ordini WHERE id = %s", (id,))
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('gestione_giorno', data=data_ritorno))

@app.route('/aggiungi', methods=['POST'])
@permission_required('can_edit')
def aggiungi_ordine():
    try:
        d = request.form
        categoria = d.get('categoria')
        if not categoria: return "Errore: La categoria è obbligatoria.", 400
        orario = d.get('orario')
        if not orario or orario.isspace(): orario = None

        foto_b64 = process_uploaded_image(request.files.get('foto'))
        foto_desc = d.get('foto_descrizione', '')

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO ordini (
                cliente, telefono, negozio, tipo_dolce, farcitura, kg, unita_misura, 
                orario, data_consegna, descrizione, categoria, operatore, canale, 
                modalita, indirizzo, pagamento_tipo, pagamento_importo, foto, foto_descrizione
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) 
            RETURNING id
        """, (
            d.get('cliente'), d.get('telefono'), d.get('negozio'), d.get('tipo_dolce'), 
            d.get('farcitura'), d.get('kg') or 0, d.get('unita_misura'), orario, 
            d.get('data_consegna'), d.get('descrizione'), categoria, d.get('operatore'), 
            d.get('canale'), d.get('modalita'), d.get('indirizzo'), d.get('pagamento_tipo'), 
            d.get('pagamento_importo') or 0, foto_b64, foto_desc
        ))
        nuovo_id = cur.fetchone()['id']
        save_or_update_client(cur, d.get('cliente'), d.get('telefono'))
        conn.commit()
        cur.close()
        conn.close()
        if d.get('azione') == 'salva_stampa': return redirect(url_for('stampa_ordine', id=nuovo_id))
        return redirect(url_for('gestione_giorno', data=d.get('data_consegna')))
    except Exception as e: return f"Errore: {e}", 500

@app.route('/api/cerca_cliente')
def api_cerca_cliente():
    q = request.args.get('q', '').strip()
    if not q or len(q) < 2:
        return jsonify([])
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        search_pattern = f"%{q}%"
        cur.execute("""
            SELECT id, nome, telefono, email, foto 
            FROM clienti 
            WHERE nome ILIKE %s OR telefono ILIKE %s 
            ORDER BY nome ASC 
            LIMIT 5
        """, (search_pattern, search_pattern))
        results = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify(results)
    except Exception as e:
        return jsonify([])


@app.route('/anagrafica')
@permission_required('can_view_anagrafica')
def anagrafica():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM clienti ORDER BY nome ASC")
    clienti = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('anagrafica.html', clienti=clienti)

@app.route('/nuovo_cliente')
@permission_required('can_view_anagrafica')
def nuovo_cliente(): return render_template('nuovo_cliente.html')

@app.route('/salva_nuovo_cliente', methods=['POST'])
@permission_required('can_view_anagrafica')
def salva_nuovo_cliente():
    nome = request.form.get('nome')
    telefono = request.form.get('telefono')
    email = request.form.get('email')
    foto_b64 = process_uploaded_image(request.files.get('foto'))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO clienti (nome, telefono, email, foto) VALUES (%s, %s, %s, %s) ON CONFLICT (telefono) DO UPDATE SET nome=EXCLUDED.nome, email=EXCLUDED.email, foto=COALESCE(EXCLUDED.foto, clienti.foto)", (nome, telefono, email, foto_b64))
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('anagrafica'))

@app.route('/api/crea_cliente_rapido', methods=['POST'])
def api_crea_cliente_rapido():
    """Crea un cliente al volo (da modale ordine/rinfresco) e ritorna JSON senza navigare via."""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Non autenticato'}), 401
    try:
        data = request.get_json(silent=True) or {}
        nome = (data.get('nome') or '').strip()
        telefono = (data.get('telefono') or '').strip()
        if not nome:
            return jsonify({'success': False, 'error': 'Il nome è obbligatorio'}), 400
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO clienti (nome, telefono, email, foto)
               VALUES (%s, %s, %s, NULL)
               ON CONFLICT (telefono) DO UPDATE
                 SET nome = EXCLUDED.nome
               RETURNING id, nome, telefono""",
            (nome, telefono or None, None)
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'success': True, 'id': row['id'], 'nome': row['nome'], 'telefono': row['telefono'] or ''})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/modifica_cliente/<int:id>', methods=['GET', 'POST'])
@permission_required('can_view_anagrafica')
def modifica_cliente(id):
    conn = get_db_connection()
    cur = conn.cursor()
    if request.method == 'POST':
        foto_nuova = process_uploaded_image(request.files.get('foto'))
        rimuovi_foto = request.form.get('rimuovi_foto') == '1'
        cur.execute("SELECT foto FROM clienti WHERE id = %s", (id,))
        old_c = cur.fetchone() or {}
        if rimuovi_foto:
            foto_finale = None
        elif foto_nuova:
            foto_finale = foto_nuova
        else:
            foto_finale = old_c.get('foto')
        cur.execute("UPDATE clienti SET nome=%s, telefono=%s, email=%s, foto=%s WHERE id=%s", (request.form.get('nome'), request.form.get('telefono'), request.form.get('email'), foto_finale, id))
        conn.commit()
        cur.close()
        conn.close()
        return redirect(url_for('anagrafica'))
    cur.execute("SELECT * FROM clienti WHERE id = %s", (id,))
    cliente = cur.fetchone()
    cur.close()
    conn.close()
    return render_template('modifica_cliente.html', c=cliente)

@app.route('/carica_foto_cliente/<int:id>', methods=['POST'])
@permission_required('can_view_anagrafica')
def carica_foto_cliente(id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        if request.form.get('rimuovi_foto') == '1':
            cur.execute("UPDATE clienti SET foto = NULL WHERE id = %s", (id,))
            conn.commit()
            cur.close()
            conn.close()
            flash("Foto del cliente rimossa.", "info")
            return redirect(url_for('anagrafica'))
        
        foto_b64 = process_uploaded_image(request.files.get('foto'))
        if foto_b64:
            cur.execute("UPDATE clienti SET foto = %s WHERE id = %s", (foto_b64, id))
            conn.commit()
            flash("Foto cliente aggiornata con successo!", "success")
        else:
            flash("Nessuna foto selezionata.", "warning")
        cur.close()
        conn.close()
    except Exception as e:
        flash(f"Errore caricamento foto: {e}", "danger")
    return redirect(url_for('anagrafica'))

@app.route('/elimina_cliente/<int:id>')
@permission_required('can_view_anagrafica')
def elimina_cliente(id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM clienti WHERE id = %s", (id,))
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('anagrafica'))

def send_email_notification_async(testo_nota, username):
    """Invia una notifica email in background a soysalsero2011@gmail.com quando viene salvata una nota di supporto."""
    def send():
        email_to = os.environ.get('NOTIFY_EMAIL_TO', 'soysalsero2011@gmail.com')
        smtp_user = os.environ.get('SMTP_USER')
        smtp_pass = os.environ.get('SMTP_PASSWORD')
        smtp_server = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
        smtp_port = int(os.environ.get('SMTP_PORT', '587'))
        data_ora_str = now_italy().strftime('%d/%m/%Y alle %H:%M')

        # 1. Tenta l'invio via SMTP standard se le credenziali sono state configurate
        if smtp_user and smtp_pass:
            try:
                msg = MIMEMultipart('alternative')
                msg['Subject'] = f"📝 Nuova Nota Supporto da {username}"
                msg['From'] = f"Gestionale Pasticceria <{smtp_user}>"
                msg['To'] = email_to

                text_body = f"Nuova Nota / Segnalazione ricevuta nel Gestionale Pasticceria\n\nUtente: {username}\nData: {data_ora_str}\n\nTesto:\n{testo_nota}\n"

                html_body = f"""
                <html>
                  <body style="font-family: Arial, sans-serif; background-color: #f8fafc; padding: 20px;">
                    <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 14px; padding: 25px; border-top: 6px solid #f59e0b; box-shadow: 0 4px 12px rgba(0,0,0,0.08);">
                      <h2 style="color: #1e293b; margin-top: 0;">📝 Nuova Nota di Supporto</h2>
                      <p style="color: #475569; font-size: 0.95rem;">È stata inserita una nuova nota o segnalazione nel gestionale.</p>
                      
                      <div style="background: #f1f5f9; padding: 14px 18px; border-radius: 10px; margin: 15px 0;">
                        <p style="margin: 0 0 6px 0; color: #334155;"><strong>Utente:</strong> {username}</p>
                        <p style="margin: 0; color: #334155;"><strong>Data:</strong> {data_ora_str}</p>
                      </div>

                      <div style="background: #fffbe8; border-left: 4px solid #f59e0b; padding: 16px; border-radius: 8px; margin-top: 15px;">
                        <p style="margin: 0; font-size: 1rem; color: #1e293b; white-space: pre-wrap;">{testo_nota}</p>
                      </div>
                      
                      <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;">
                      <small style="color: #94a3b8;">Email automatica dal Gestionale Pasticceria</small>
                    </div>
                  </body>
                </html>
                """

                msg.attach(MIMEText(text_body, 'plain', 'utf-8'))
                msg.attach(MIMEText(html_body, 'html', 'utf-8'))

                if smtp_port == 465:
                    server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=12)
                    server.login(smtp_user, smtp_pass)
                    server.sendmail(smtp_user, [email_to], msg.as_string())
                    server.quit()
                else:
                    server = smtplib.SMTP(smtp_server, smtp_port, timeout=12)
                    server.starttls()
                    server.login(smtp_user, smtp_pass)
                    server.sendmail(smtp_user, [email_to], msg.as_string())
                    server.quit()

                print(f"✅ Notifica email via SMTP inviata con successo a {email_to}")
                return
            except Exception as e:
                print(f"⚠️ Errore invio SMTP: {e}. Tenta invio diretto HTTP API...")

        # 2. Invio automatico via HTTP API verso l'indirizzo email specificato
        try:
            import urllib.request
            import json
            url = f"https://formsubmit.co/ajax/{email_to}"
            payload = json.dumps({
                "_subject": f"📝 Nuova Nota Supporto da {username}",
                "Utente": username,
                "Data": data_ora_str,
                "Messaggio": testo_nota,
                "_template": "table"
            }).encode('utf-8')

            req = urllib.request.Request(
                url, 
                data=payload, 
                headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                    'Origin': 'https://gestionale-pasticceria.onrender.com',
                    'Referer': 'https://gestionale-pasticceria.onrender.com/'
                }
            )
            res = urllib.request.urlopen(req, timeout=10)
            res_body = res.read().decode('utf-8')
            print(f"✅ Notifica email HTTP inviata con successo a {email_to}: {res_body}")
        except Exception as e_http:
            print(f"❌ Errore durante l'invio della notifica email HTTP: {e_http}")

        # 3. Invio notifica Push immediata gratuita su Ntfy
        try:
            import urllib.request
            ntfy_url = "https://ntfy.sh/gestionale_secondo_negozio_note"
            ntfy_msg = f"Utente: {username}\nData: {data_ora_str}\n\nNota:\n{testo_nota}".encode('utf-8')
            ntfy_req = urllib.request.Request(
                ntfy_url,
                data=ntfy_msg,
                headers={
                    'Title': f"Nuova Nota Supporto da {username}",
                    'Tags': 'memo,bell'
                }
            )
            urllib.request.urlopen(ntfy_req, timeout=8)
            print("✅ Notifica Push Ntfy inviata con successo")
        except Exception as e_ntfy:
            print(f"⚠️ Errore notifica Push Ntfy: {e_ntfy}")

    threading.Thread(target=send, daemon=True).start()

@app.route('/supporto')
def supporto():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM supporto_notes ORDER BY data_creazione DESC")
    notes = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('supporto.html', notes=notes)

@app.route('/salva_nota_supporto', methods=['POST'])
def salva_nota_supporto():
    testo_nota = request.form.get('testo')
    username = session.get('username', 'Utente Sconosciuto')
    if testo_nota and testo_nota.strip():
        conn = get_db_connection()
        cur = conn.cursor()
        now_it = now_italy()
        cur.execute("INSERT INTO supporto_notes (testo, autore, data_creazione) VALUES (%s, %s, %s)", (testo_nota.strip(), username, now_it))
        conn.commit()
        cur.close()
        conn.close()
        send_email_notification_async(testo_nota, username)
        flash("Nota aggiunta con successo.", "success")
    return redirect(url_for('supporto'))

@app.route('/modifica_nota_supporto/<int:id>', methods=['POST'])
def modifica_nota_supporto(id):
    username = session.get('username', '')
    nuovo_testo = request.form.get('testo')
    if not nuovo_testo or not nuovo_testo.strip():
        flash("Il testo della nota non può essere vuoto.", "warning")
        return redirect(url_for('supporto'))
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT autore FROM supporto_notes WHERE id = %s", (id,))
    note = cur.fetchone()
    
    if not note:
        cur.close()
        conn.close()
        flash("Nota non trovata.", "danger")
        return redirect(url_for('supporto'))
    
    note_autore = (note.get('autore') or '').strip().lower()
    curr_user = username.strip().lower()
    
    if not note_autore or note_autore != curr_user:
        cur.close()
        conn.close()
        flash("Non hai i permessi per modificare questa nota. Può essere modificata solo dall'utente che l'ha inserita.", "danger")
        return redirect(url_for('supporto'))
    
    cur.execute("UPDATE supporto_notes SET testo = %s WHERE id = %s", (nuovo_testo.strip(), id))
    conn.commit()
    cur.close()
    conn.close()
    flash("Nota modificata con successo.", "success")
    return redirect(url_for('supporto'))

@app.route('/elimina_nota_supporto/<int:id>')
def elimina_nota_supporto(id):
    username = session.get('username', '')
    if username.strip().lower() != 'emanuele':
        flash("Non hai i permessi. Le note possono essere cancellate solamente dall'utenza Emanuele.", "danger")
        return redirect(url_for('supporto'))
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM supporto_notes WHERE id = %s", (id,))
    conn.commit()
    cur.close()
    conn.close()
    flash("Nota eliminata con successo.", "success")
    return redirect(url_for('supporto'))

# -------------------- PIANO FERIE --------------------
@app.route('/piano-ferie')
def piano_ferie():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    now_curr = now_italy()
    mese = int(request.args.get('mese', now_curr.month))
    anno = int(request.args.get('anno', now_curr.year))
    filtro_sede = request.args.get('sede', 'tutte').strip().lower()

    prev_mese, prev_anno = (mese - 1, anno) if mese > 1 else (12, anno - 1)
    next_mese, next_anno = (mese + 1, anno) if mese < 12 else (1, anno + 1)

    cal = calendar.Calendar(firstweekday=0)
    month_dates = cal.monthdatescalendar(anno, mese)
    min_date = month_dates[0][0]
    max_date = month_dates[-1][-1]

    conn = get_db_connection()
    cur = conn.cursor()
    # Verifica che la tabella e colonne esistano
    cur.execute("""
        CREATE TABLE IF NOT EXISTS piano_ferie (
            id SERIAL PRIMARY KEY,
            nome TEXT NOT NULL,
            sede TEXT NOT NULL,
            data_inizio DATE NOT NULL,
            data_fine DATE NOT NULL,
            note TEXT,
            creato_da TEXT,
            creato_il TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            confermato BOOLEAN DEFAULT FALSE,
            confermato_da TEXT,
            confermato_il TIMESTAMP WITH TIME ZONE
        );
    """)
    cur.execute("ALTER TABLE piano_ferie ADD COLUMN IF NOT EXISTS confermato BOOLEAN DEFAULT FALSE;")
    cur.execute("ALTER TABLE piano_ferie ADD COLUMN IF NOT EXISTS confermato_da TEXT;")
    cur.execute("ALTER TABLE piano_ferie ADD COLUMN IF NOT EXISTS confermato_il TIMESTAMP WITH TIME ZONE;")
    cur.execute("ALTER TABLE piano_ferie ADD COLUMN IF NOT EXISTS tipo_evento TEXT DEFAULT 'ferie';")
    cur.execute("ALTER TABLE piano_ferie ADD COLUMN IF NOT EXISTS orario_richiesto TEXT DEFAULT '';")
    cur.execute("ALTER TABLE piano_ferie ADD COLUMN IF NOT EXISTS rifiutato BOOLEAN DEFAULT FALSE;")
    cur.execute("ALTER TABLE piano_ferie ADD COLUMN IF NOT EXISTS rifiutato_da TEXT;")
    cur.execute("ALTER TABLE piano_ferie ADD COLUMN IF NOT EXISTS rifiutato_il TIMESTAMP WITH TIME ZONE;")
    conn.commit()

    cur.execute("""
        SELECT * FROM piano_ferie 
        WHERE data_inizio <= %s AND data_fine >= %s
        ORDER BY data_inizio ASC, nome ASC
    """, (max_date, min_date))
    ferie_all = cur.fetchall()

    cur.execute("SELECT DISTINCT nome FROM piano_ferie ORDER BY nome ASC")
    dipendenti_rows = cur.fetchall()
    dipendenti_suggeriti = [r['nome'] for r in dipendenti_rows if r.get('nome')]

    cur.close()
    conn.close()

    if filtro_sede in ['ostia', 'palocco', 'secondo negozio']:
        ferie_filtrate = [f for f in ferie_all if (f['sede'].strip().lower() in ['palocco', 'secondo negozio'] if filtro_sede in ['palocco', 'secondo negozio'] else f['sede'].strip().lower() == filtro_sede)]
    else:
        ferie_filtrate = ferie_all

    curr_username = (session.get('username') or '').strip().lower()
    can_manage_ferie = (
        curr_username in ['roberto', 'emanuele', 'admin'] or
        session.get('admin_verified', False) or
        bool((session.get('permissions') or {}).get('can_manage_users', False))
    )

    tabellone = []
    conteggio_ostia_mese = set()
    conteggio_palocco_mese = set()
    conteggio_in_attesa_mese = set()

    for sett in month_dates:
        settimana_list = []
        for g in sett:
            g_ferie = [f for f in ferie_filtrate if f['data_inizio'] <= g <= f['data_fine']]
            ostia_count = sum(1 for f in g_ferie if f['sede'].strip().lower() == 'ostia' and f.get('confermato'))
            palocco_count = sum(1 for f in g_ferie if f['sede'].strip().lower() in ['palocco', 'secondo negozio'] and f.get('confermato'))
            # Non contare i rifiutati nel contatore "In Attesa"
            attesa_count = sum(1 for f in g_ferie if not f.get('confermato') and not f.get('rifiutato'))
            
            g_ferie_json = []
            for f in g_ferie:
                is_conf = bool(f.get('confermato', False))
                g_ferie_json.append({
                    'id': f['id'],
                    'nome': f['nome'],
                    'sede': f['sede'],
                    'confermato': is_conf,
                    'confermato_da': f.get('confermato_da') or '',
                    'rifiutato': bool(f.get('rifiutato', False)),
                    'rifiutato_da': f.get('rifiutato_da') or '',
                    'creato_da': f.get('creato_da') or '',
                    'data_inizio': f['data_inizio'].strftime('%d/%m/%Y') if hasattr(f['data_inizio'], 'strftime') else str(f['data_inizio']),
                    'data_fine': f['data_fine'].strftime('%d/%m/%Y') if hasattr(f['data_fine'], 'strftime') else str(f['data_fine']),
                    'data_inizio_iso': f['data_inizio'].strftime('%Y-%m-%d') if hasattr(f['data_inizio'], 'strftime') else str(f['data_inizio']),
                    'data_fine_iso': f['data_fine'].strftime('%Y-%m-%d') if hasattr(f['data_fine'], 'strftime') else str(f['data_fine']),
                    'note': f.get('note') or '',
                    'tipo_evento': f.get('tipo_evento') or 'ferie',
                    'orario_richiesto': f.get('orario_richiesto') or ''
                })

            if g.month == mese:
                for f in g_ferie:
                    # I rifiutati non contano né in attesa né confermati
                    if f.get('rifiutato'):
                        pass
                    elif not f.get('confermato'):
                        conteggio_in_attesa_mese.add((f['id'], f['nome']))
                    elif f['sede'].strip().lower() == 'ostia':
                        conteggio_ostia_mese.add((f['id'], f['nome']))
                    elif f['sede'].strip().lower() in ['palocco', 'secondo negozio']:
                        conteggio_palocco_mese.add((f['id'], f['nome']))

            settimana_list.append({
                'data': g.strftime('%Y-%m-%d'),
                'giorno_num': g.day,
                'fuori_mese': g.month != mese,
                'ferie': g_ferie,
                'ferie_json': g_ferie_json,
                'ostia_count': ostia_count,
                'palocco_count': palocco_count,
                'attesa_count': attesa_count,
                'totale_ferie': len(g_ferie),
                'has_conflict': (ostia_count > 1 or palocco_count > 1)
            })
        tabellone.append(settimana_list)

    first_day_month = date(anno, mese, 1)
    last_day_month = date(anno, mese, calendar.monthrange(anno, mese)[1])
    ferie_mese_raw = [f for f in ferie_all if not (f['data_fine'] < first_day_month or f['data_inizio'] > last_day_month)]
    
    ferie_mese_list = []
    for f in ferie_mese_raw:
        f_dict = dict(f)
        f_dict['confermato'] = bool(f.get('confermato', False))
        f_dict['confermato_da'] = f.get('confermato_da') or ''
        f_dict['creato_da'] = f.get('creato_da') or ''
        f_dict['data_inizio_iso'] = f['data_inizio'].strftime('%Y-%m-%d') if hasattr(f['data_inizio'], 'strftime') else str(f['data_inizio'])
        f_dict['data_fine_iso'] = f['data_fine'].strftime('%Y-%m-%d') if hasattr(f['data_fine'], 'strftime') else str(f['data_fine'])
        f_dict['data_inizio_it'] = f['data_inizio'].strftime('%d/%m/%Y') if hasattr(f['data_inizio'], 'strftime') else str(f['data_inizio'])
        f_dict['data_fine_it'] = f['data_fine'].strftime('%d/%m/%Y') if hasattr(f['data_fine'], 'strftime') else str(f['data_fine'])
        f_dict['tipo_evento'] = f.get('tipo_evento') or 'ferie'
        f_dict['orario_richiesto'] = f.get('orario_richiesto') or ''
        f_dict['rifiutato'] = bool(f.get('rifiutato', False))
        f_dict['rifiutato_da'] = f.get('rifiutato_da') or ''
        ferie_mese_list.append(f_dict)

    mesi = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]

    return render_template(
        'piano_ferie.html',
        tabellone=tabellone,
        mese_nome=mesi[mese-1],
        mese=mese,
        anno=anno,
        prev_mese=prev_mese,
        prev_anno=prev_anno,
        next_mese=next_mese,
        next_anno=next_anno,
        oggi=now_curr.date().strftime('%Y-%m-%d'),
        filtro_sede=filtro_sede,
        conteggio_ostia=len(conteggio_ostia_mese),
        conteggio_palocco=len(conteggio_palocco_mese),
        conteggio_attesa=len(conteggio_in_attesa_mese),
        elenco_ferie_mese=ferie_mese_list,
        dipendenti_suggeriti=dipendenti_suggeriti,
        can_manage_ferie=can_manage_ferie,
        curr_username=curr_username
    )

@app.route('/piano-ferie/salva', methods=['POST'])
def salva_piano_ferie():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    nome = request.form.get('nome', '').strip()
    sede = request.form.get('sede', '').strip()
    data_inizio_str = request.form.get('data_inizio', '').strip()
    data_fine_str = request.form.get('data_fine', '').strip()
    note = request.form.get('note', '').strip()
    tipo_evento = request.form.get('tipo_evento', 'ferie').strip()  # 'ferie', 'giorno_libero' o 'malattia'
    orario_richiesto = request.form.get('orario_richiesto', '').strip() if tipo_evento == 'giorno_libero' else ''
    mese_ritorno = request.form.get('mese_ritorno')
    anno_ritorno = request.form.get('anno_ritorno')

    if not nome:
        flash("Inserire il nome del dipendente.", "danger")
        return redirect(url_for('piano_ferie', mese=mese_ritorno, anno=anno_ritorno))

    if sede.lower() not in ['ostia', 'palocco', 'secondo negozio']:
        flash("Selezionare la sede (Ostia o Secondo negozio).", "danger")
        return redirect(url_for('piano_ferie', mese=mese_ritorno, anno=anno_ritorno))

    try:
        data_inizio = datetime.strptime(data_inizio_str, '%Y-%m-%d').date()
        data_fine = datetime.strptime(data_fine_str, '%Y-%m-%d').date()
    except Exception:
        flash("Date non valide.", "danger")
        return redirect(url_for('piano_ferie', mese=mese_ritorno, anno=anno_ritorno))

    if data_fine < data_inizio:
        flash("La data di fine non può essere precedente alla data di inizio.", "danger")
        return redirect(url_for('piano_ferie', mese=mese_ritorno, anno=anno_ritorno))

    nome_norm = nome.title()
    sede_norm = 'Ostia' if sede.lower() == 'ostia' else 'Secondo negozio'
    username = session.get('username', 'Utente')
    is_admin = username.strip().lower() in ['roberto', 'emanuele']
    
    # La malattia viene auto-confermata da chiunque la inserisca
    auto_confirm = is_admin or (tipo_evento == 'malattia')
    confermato = auto_confirm
    confermato_da = username if auto_confirm else None
    confermato_il = now_italy() if auto_confirm else None

    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT * FROM piano_ferie 
        WHERE LOWER(nome) = LOWER(%s)
          AND data_inizio <= %s 
          AND data_fine >= %s
    """, (nome_norm, data_fine, data_inizio))
    sovrapposti = cur.fetchall()

    if sovrapposti:
        periodi = ", ".join([f"{s['data_inizio'].strftime('%d/%m')} - {s['data_fine'].strftime('%d/%m')}" for s in sovrapposti])
        flash(f"Avviso: {nome_norm} ha già altre ferie registrate nel periodo ({periodi}).", "warning")
    
    cur.execute("""
        INSERT INTO piano_ferie (nome, sede, data_inizio, data_fine, note, creato_da, confermato, confermato_da, confermato_il, tipo_evento, orario_richiesto)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (nome_norm, sede_norm, data_inizio, data_fine, note, username, confermato, confermato_da, confermato_il, tipo_evento, orario_richiesto))
    
    if confermato:
        _sincronizza_turni_da_piano_ferie(cur, data_inizio, data_fine)

    conn.commit()
    cur.close()
    conn.close()

    if tipo_evento == 'giorno_libero':
        etichetta = 'Giorno con Orario'
    elif tipo_evento == 'malattia':
        etichetta = 'Malattia'
    else:
        etichetta = 'Ferie'
    if tipo_evento == 'malattia':
        flash(f"Malattia per {nome_norm} ({sede_norm}) registrata e confermata automaticamente. I turni sono stati aggiornati.", "success")
    elif is_admin:
        flash(f"{etichetta} per {nome_norm} ({sede_norm}) registrate e confermate!", "success")
    else:
        flash(f"Richiesta {etichetta.lower()} per {nome_norm} ({sede_norm}) inserita in attesa di conferma da Roberto.", "info")
    return redirect(url_for('piano_ferie', mese=mese_ritorno or data_inizio.month, anno=anno_ritorno or data_inizio.year))

@app.route('/piano-ferie/conferma/<int:id>', methods=['POST', 'GET'])
def conferma_piano_ferie(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    username = (session.get('username') or 'Admin').strip()
    curr_username = username.lower()
    is_admin = (
        curr_username in ['roberto', 'emanuele', 'admin'] or
        session.get('admin_verified', False) or
        bool((session.get('permissions') or {}).get('can_manage_users', False))
    )
    if not is_admin:
        flash("Non hai i permessi. Solo Roberto può confermare le ferie.", "danger")
        return redirect(url_for('piano_ferie'))

    mese_ritorno = request.args.get('mese') or request.form.get('mese_ritorno')
    anno_ritorno = request.args.get('anno') or request.form.get('anno_ritorno')

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT nome, sede, data_inizio, data_fine, tipo_evento, orario_richiesto FROM piano_ferie WHERE id = %s", (id,))
    f = cur.fetchone()
    nome = f['nome'] if f else 'Dipendente'

    cur.execute("""
        UPDATE piano_ferie 
        SET confermato = TRUE, confermato_da = %s, confermato_il = %s
        WHERE id = %s
    """, (username, now_italy(), id))

    if f and (f['sede'] or '').lower() in ('ostia', 'palocco', 'secondo negozio'):
        _sincronizza_turni_da_piano_ferie(cur, f['data_inizio'], f['data_fine'])

    conn.commit()
    cur.close()
    conn.close()

    flash(f"Richiesta di {nome} confermata con successo! I turni sono stati aggiornati automaticamente.", "success")
    return redirect(url_for('piano_ferie', mese=mese_ritorno, anno=anno_ritorno))

@app.route('/piano-ferie/rifiuta/<int:id>', methods=['POST', 'GET'])
def rifiuta_piano_ferie(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    username = (session.get('username') or 'Admin').strip()
    curr_username = username.lower()
    is_admin = (
        curr_username in ['roberto', 'emanuele', 'admin'] or
        session.get('admin_verified', False) or
        bool((session.get('permissions') or {}).get('can_manage_users', False))
    )
    if not is_admin:
        flash("Non hai i permessi. Solo Roberto ed Emanuele possono rifiutare le richieste.", "danger")
        return redirect(url_for('piano_ferie'))

    mese_ritorno = request.args.get('mese') or request.form.get('mese_ritorno')
    anno_ritorno = request.args.get('anno') or request.form.get('anno_ritorno')

    conn = get_db_connection()
    cur = conn.cursor()

    # Assicura colonne rifiuto esistano
    cur.execute("ALTER TABLE piano_ferie ADD COLUMN IF NOT EXISTS rifiutato BOOLEAN DEFAULT FALSE;")
    cur.execute("ALTER TABLE piano_ferie ADD COLUMN IF NOT EXISTS rifiutato_da TEXT;")
    cur.execute("ALTER TABLE piano_ferie ADD COLUMN IF NOT EXISTS rifiutato_il TIMESTAMP WITH TIME ZONE;")

    cur.execute("SELECT nome, sede, data_inizio, data_fine FROM piano_ferie WHERE id = %s", (id,))
    f = cur.fetchone()
    nome = f['nome'] if f else 'Dipendente'

    cur.execute("""
        UPDATE piano_ferie 
        SET confermato = FALSE,
            confermato_da = NULL,
            confermato_il = NULL,
            rifiutato = TRUE,
            rifiutato_da = %s,
            rifiutato_il = %s
        WHERE id = %s
    """, (username, now_italy(), id))

    # Ri-sincronizza i turni (rimuove l'impatto di questa richiesta)
    if f and (f.get('sede') or '').lower() in ('ostia', 'palocco', 'secondo negozio'):
        _sincronizza_turni_da_piano_ferie(cur, f['data_inizio'], f['data_fine'])

    conn.commit()
    cur.close()
    conn.close()

    flash(f"Richiesta di {nome} segnata come NON APPROVATA da {username}.", "warning")
    return redirect(url_for('piano_ferie', mese=mese_ritorno, anno=anno_ritorno))

@app.route('/piano-ferie/sospendi/<int:id>', methods=['POST', 'GET'])
def sospendi_piano_ferie(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    curr_username = (session.get('username') or '').strip().lower()
    is_admin = (
        curr_username in ['roberto', 'emanuele', 'admin'] or
        session.get('admin_verified', False) or
        bool((session.get('permissions') or {}).get('can_manage_users', False))
    )
    if not is_admin:
        flash("Non hai i permessi. Solo Roberto può modificare lo stato di conferma.", "danger")
        return redirect(url_for('piano_ferie'))

    mese_ritorno = request.args.get('mese') or request.form.get('mese_ritorno')
    anno_ritorno = request.args.get('anno') or request.form.get('anno_ritorno')

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT sede, data_inizio, data_fine FROM piano_ferie WHERE id = %s", (id,))
    f = cur.fetchone()
    cur.execute("""UPDATE piano_ferie 
        SET confermato = FALSE, confermato_da = NULL, confermato_il = NULL,
            rifiutato = FALSE, rifiutato_da = NULL, rifiutato_il = NULL
        WHERE id = %s""", (id,))
    if f and (f.get('sede') or '').lower() in ('ostia', 'palocco', 'secondo negozio'):
        _sincronizza_turni_da_piano_ferie(cur, f['data_inizio'], f['data_fine'])
    conn.commit()
    cur.close()
    conn.close()

    flash("Ferie reimpostate in attesa di conferma (in grigio). I turni sono stati ripristinati.", "info")
    return redirect(url_for('piano_ferie', mese=mese_ritorno, anno=anno_ritorno))

@app.route('/piano-ferie/modifica/<int:id>', methods=['POST'])
def modifica_piano_ferie(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    nome = request.form.get('nome', '').strip()
    sede = request.form.get('sede', '').strip()
    data_inizio_str = request.form.get('data_inizio', '').strip()
    data_fine_str = request.form.get('data_fine', '').strip()
    note = request.form.get('note', '').strip()
    tipo_evento = request.form.get('tipo_evento', 'ferie').strip()
    orario_richiesto = request.form.get('orario_richiesto', '').strip() if tipo_evento == 'giorno_libero' else ''
    mese_ritorno = request.form.get('mese_ritorno')
    anno_ritorno = request.form.get('anno_ritorno')

    if not nome or sede.lower() not in ['ostia', 'palocco', 'secondo negozio']:
        flash("Dati inseriti non validi.", "danger")
        return redirect(url_for('piano_ferie', mese=mese_ritorno, anno=anno_ritorno))

    try:
        data_inizio = datetime.strptime(data_inizio_str, '%Y-%m-%d').date()
        data_fine = datetime.strptime(data_fine_str, '%Y-%m-%d').date()
    except Exception:
        flash("Date non valide.", "danger")
        return redirect(url_for('piano_ferie', mese=mese_ritorno, anno=anno_ritorno))

    if data_fine < data_inizio:
        flash("La data di fine non può essere precedente alla data di inizio.", "danger")
        return redirect(url_for('piano_ferie', mese=mese_ritorno, anno=anno_ritorno))

    nome_norm = nome.title()
    sede_norm = 'Ostia' if sede.lower() == 'ostia' else 'Secondo negozio'

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT confermato, confermato_da, confermato_il, data_inizio, data_fine FROM piano_ferie WHERE id = %s", (id,))
    existing = cur.fetchone()
    old_confermato = bool(existing.get('confermato')) if existing else False
    old_start = existing['data_inizio'] if existing else data_inizio
    old_end = existing['data_fine'] if existing else data_fine

    username = session.get('username', 'Utente')
    curr_u = username.strip().lower()
    is_admin = (
        curr_u in ['roberto', 'emanuele', 'admin'] or
        session.get('admin_verified', False) or
        bool((session.get('permissions') or {}).get('can_manage_users', False))
    )
    auto_confirm = is_admin or (tipo_evento == 'malattia')
    confermato = old_confermato or auto_confirm
    conf_da = username if (auto_confirm and not old_confermato) else existing.get('confermato_da')
    conf_il = now_italy() if (auto_confirm and not old_confermato) else existing.get('confermato_il')

    cur.execute("""
        UPDATE piano_ferie 
        SET nome = %s, sede = %s, data_inizio = %s, data_fine = %s, note = %s, tipo_evento = %s, orario_richiesto = %s,
            confermato = %s, confermato_da = %s, confermato_il = %s
        WHERE id = %s
    """, (nome_norm, sede_norm, data_inizio, data_fine, note, tipo_evento, orario_richiesto, confermato, conf_da, conf_il, id))

    if confermato or auto_confirm:
        min_d = min(old_start, data_inizio)
        max_d = max(old_end, data_fine)
        _sincronizza_turni_da_piano_ferie(cur, min_d, max_d)

    conn.commit()
    cur.close()
    conn.close()

    flash(f"Richiesta di {nome_norm} aggiornata con successo! I turni sono stati sincronizzati.", "success")
    return redirect(url_for('piano_ferie', mese=mese_ritorno or data_inizio.month, anno=anno_ritorno or data_inizio.year))

@app.route('/piano-ferie/elimina/<int:id>', methods=['POST', 'GET'])
def elimina_piano_ferie(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    curr_username = (session.get('username') or '').strip().lower()
    mese_ritorno = request.args.get('mese') or request.form.get('mese_ritorno')
    anno_ritorno = request.args.get('anno') or request.form.get('anno_ritorno')

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT nome, creato_da, sede, data_inizio, data_fine, confermato FROM piano_ferie WHERE id = %s", (id,))
    f = cur.fetchone()
    
    if not f:
        cur.close()
        conn.close()
        flash("Record ferie non trovato.", "warning")
        return redirect(url_for('piano_ferie', mese=mese_ritorno, anno=anno_ritorno))

    creato_da_lower = (f.get('creato_da') or '').strip().lower()
    is_admin = (
        curr_username in ['roberto', 'emanuele', 'admin'] or
        session.get('admin_verified', False) or
        bool((session.get('permissions') or {}).get('can_manage_users', False))
    )
    is_creator = (curr_username == creato_da_lower and curr_username != '')
    has_permission = is_admin or is_creator or (not creato_da_lower)

    if not has_permission:
        cur.close()
        conn.close()
        flash("Non hai i permessi per eliminare queste ferie. Possono essere eliminate solo da chi le ha inserite, oppure da Roberto ed Emanuele.", "danger")
        return redirect(url_for('piano_ferie', mese=mese_ritorno, anno=anno_ritorno))

    cur.execute("DELETE FROM piano_ferie WHERE id = %s", (id,))
    if f and (f.get('sede') or '').lower() in ('ostia', 'palocco', 'secondo negozio') and f.get('confermato'):
        _sincronizza_turni_da_piano_ferie(cur, f['data_inizio'], f['data_fine'])
    conn.commit()
    cur.close()
    conn.close()

    flash(f"Ferie di {f['nome']} eliminate con successo. I turni sono stati ripristinati.", "success")
    return redirect(url_for('piano_ferie', mese=mese_ritorno, anno=anno_ritorno))

@app.route('/api/piano-ferie/notifiche')
def api_piano_ferie_notifiche():
    if 'user_id' not in session:
        return jsonify({'count': 0, 'status': 'unauthorized'}), 401
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM piano_ferie WHERE confermato = FALSE AND COALESCE(rifiutato, FALSE) = FALSE")
        row = cur.fetchone()
        cur.close()
        conn.close()
        count = int(row['c']) if row and row.get('c') is not None else 0
        return jsonify({'count': count, 'status': 'ok'})
    except Exception as e:
        return jsonify({'count': 0, 'error': str(e)}), 500

# ==============================================================
# TURNI SETTIMANALI
# ==============================================================

TURNI_STAFF_COLORS = {}
TURNI_NOMI = []

TURNI_PALOCCO_STAFF_COLORS = {}
TURNI_PALOCCO_NOMI = []

TURNI_DEF = {
    'feriale': [
        {'id': '6:30-14',        'label': '6:30 – 14:00'},
        {'id': '7-14',           'label': '7:00 – 14:00'},
        {'id': '14-20:30-a',     'label': '14:00 – 20:30'},
        {'id': '14-20:30-b',     'label': '14:00 – 20:30'},
    ],
    'sabato': [
        {'id': '6:30-14',        'label': '6:30 – 14:00'},
        {'id': '7-14',           'label': '7:00 – 14:00'},
        {'id': '8-13/16-20:30',  'label': '8:00–13:00 / 16:00–20:30'},
        {'id': '14-20:30',       'label': '14:00 – 20:30'},
    ],
    'domenica': [
        {'id': '6:30-14',  'label': '6:30 – 14:00'},
        {'id': '7-14-a',   'label': '7:00 – 14:00'},
        {'id': '7-14-b',   'label': '7:00 – 14:00'},
        {'id': '7-14-c',   'label': '7:00 – 14:00'},
    ],
}

TURNI_PALOCCO_DEF = {
    'feriale': [
        {'id': '6:30-14',        'label': '6:30 – 14:00'},
        {'id': '7-14',           'label': '7:00 – 14:00'},
        {'id': '14-20:30-a',     'label': '14:00 – 20:30'},
        {'id': '14-20:30-b',     'label': '14:00 – 20:30'},
    ],
    'sabato': [
        {'id': '6:30-14',        'label': '6:30 – 14:00'},
        {'id': '7-14-a',         'label': '7:00 – 14:00'},
        {'id': '7-14-b',         'label': '7:00 – 14:00'},
        {'id': '8-13/16-20:30',  'label': '8:00–13:00 / 16:00–20:30'},
        {'id': '14-20:30-a',     'label': '14:00 – 20:30'},
        {'id': '14-20:30-b',     'label': '14:00 – 20:30'},
    ],
    'domenica': [
        {'id': '6:30-14',        'label': '6:30 – 14:00'},
        {'id': '7-14-a',         'label': '7:00 – 14:00'},
        {'id': '7-14-b',         'label': '7:00 – 14:00'},
        {'id': '7-14-c',         'label': '7:00 – 14:00'},
        {'id': '14-20:30-a',     'label': '14:00 – 20:30'},
        {'id': '14-20:30-b',     'label': '14:00 – 20:30'},
    ],
}

MESI_IT_TURNI = ['','Gennaio','Febbraio','Marzo','Aprile','Maggio','Giugno',
                 'Luglio','Agosto','Settembre','Ottobre','Novembre','Dicembre']

def _get_tipo_giorno_turni(dow):
    if dow == 6: return 'domenica'
    if dow == 5: return 'sabato'
    return 'feriale'

def _is_dipendente_disponibile(name, in_ferie_set):
    """
    Verifica se il dipendente è disponibile, controllando corrispondenze esatte
    oppure per nome di battesimo (es. 'Federica' corrisponde a 'Federica Rossi' o 'Federica').
    """
    if not name or not in_ferie_set:
        return bool(name)
    nl = name.strip().lower()
    for f in in_ferie_set:
        fl = f.strip().lower()
        if not fl:
            continue
        if nl == fl or nl in fl.split() or fl in nl.split() or fl.startswith(nl) or nl.startswith(fl):
            return False
    return True

def _auto_genera_turni(anno, mese, martedi_aperto, ferie_by_day=None, barbara_inclusa=False):
    from datetime import date
    import calendar as cal_mod
    # Usa la lista nomi configurata (vuota = nessun nome auto-assegnato)
    pool = list(TURNI_NOMI)
    rot_pool = pool[:2] if len(pool) >= 2 else pool
    result = {}
    num_days = cal_mod.monthrange(anno, mese)[1]
    for day_num in range(1, num_days + 1):
        d = date(anno, mese, day_num)
        d_str = d.isoformat()
        dow = d.weekday()
        if dow == 1 and not martedi_aperto:
            result[d_str] = {'tipo': 'chiuso', 'assegnazioni': {}}
            continue
        tipo = _get_tipo_giorno_turni(dow)
        # Con lista vuota tutti gli slot restano vuoti; lo staff va assegnato manualmente
        day_ass = {}
        if tipo == 'feriale':
            for slot in ['6:30-14', '7-14', '14-20:30-a', '14-20:30-b']:
                day_ass[slot] = ''
        elif tipo == 'sabato':
            for slot in ['6:30-14', '7-14', '8-13/16-20:30', '14-20:30']:
                day_ass[slot] = ''
        elif tipo == 'domenica':
            for slot in ['6:30-14', '7-14-a', '7-14-b', '7-14-c']:
                day_ass[slot] = ''
        result[d_str] = {'tipo': tipo, 'assegnazioni': day_ass}
    return result


def _get_marisa_ostia_days(cur, anno, mese, primo, ultimo):
    from datetime import timedelta
    cur.execute("""
        SELECT DISTINCT data::text 
        FROM turni_assegnazioni 
        WHERE data >= %s AND data <= %s AND LOWER(nome) = 'marisa' AND COALESCE(nome, '') != ''
    """, (primo, ultimo))
    rows = cur.fetchall()
    if rows:
        return set(r['data'] for r in rows)

    try:
        cur.execute("SELECT martedi_aperto, barbara_inclusa FROM turni_config WHERE anno=%s AND mese=%s", (anno, mese))
        cfg = cur.fetchone()
        marti_ostia = bool(cfg['martedi_aperto']) if cfg else False
        barb_ostia = bool(cfg['barbara_inclusa']) if cfg else False

        cur.execute("""
            SELECT nome, data_inizio, data_fine, tipo_evento, orario_richiesto 
            FROM piano_ferie 
            WHERE LOWER(sede) = 'ostia' 
              AND confermato = TRUE
              AND data_inizio <= %s 
              AND data_fine >= %s
        """, (ultimo, primo))
        ferie_ostia = {}
        for r in cur.fetchall():
            fn = (r['nome'] or '').strip().lower()
            if not fn:
                continue
            cur_d = max(r['data_inizio'], primo)
            end_d = min(r['data_fine'], ultimo)
            while cur_d <= end_d:
                ferie_ostia.setdefault(cur_d.isoformat(), set()).add(fn)
                cur_d += timedelta(days=1)

        ostia_sched = _auto_genera_turni(anno, mese, marti_ostia, ferie_by_day=ferie_ostia, barbara_inclusa=barb_ostia)
        m_days = set()
        for d_str, val in ostia_sched.items():
            ass = val.get('assegnazioni', {})
            if any((v or '').strip().lower() == 'marisa' for v in ass.values()):
                m_days.add(d_str)
        return m_days
    except Exception:
        return set()


def _auto_genera_turni_palocco(anno, mese, martedi_aperto, ferie_by_day=None, marisa_ostia_days=None, mariapia_inclusa=False):
    from datetime import date
    import calendar as cal_mod

    if marisa_ostia_days is None:
        marisa_ostia_days = set()

    # Usa la lista nomi configurata (vuota = nessun nome auto-assegnato)
    result = {}
    num_days = cal_mod.monthrange(anno, mese)[1]

    for day_num in range(1, num_days + 1):
        d = date(anno, mese, day_num)
        d_str = d.isoformat()
        dow = d.weekday()
        if dow == 1 and not martedi_aperto:
            result[d_str] = {'tipo': 'chiuso', 'assegnazioni': {}}
            continue
        tipo = _get_tipo_giorno_turni(dow)
        # Con lista vuota tutti gli slot restano vuoti; lo staff va assegnato manualmente
        day_ass = {}
        if tipo == 'feriale':
            for slot in ['6:30-14', '7-14', '14-20:30-a', '14-20:30-b']:
                day_ass[slot] = ''
        elif tipo == 'sabato':
            for slot in ['6:30-14', '8-13/16-20:30', '7-14-a', '7-14-b', '14-20:30-a', '14-20:30-b']:
                day_ass[slot] = ''
        elif tipo == 'domenica':
            for slot in ['6:30-14', '7-14-a', '7-14-b', '7-14-c', '14-20:30-a', '14-20:30-b']:
                day_ass[slot] = ''
        result[d_str] = {'tipo': tipo, 'assegnazioni': day_ass}
    return result



def _sincronizza_turni_da_piano_ferie(cur, data_inizio, data_fine):
    """
    Sincronizza e aggiorna automaticamente in turni_assegnazioni e turni_palocco_assegnazioni i giorni coinvolti
    quando viene inserita, confermata, modificata, sospesa o eliminata una richiesta nel Piano Ferie.
    """
    from datetime import date, datetime, timedelta
    import calendar as cal_mod

    if not data_inizio or not data_fine:
        return

    if isinstance(data_inizio, str):
        data_inizio = datetime.strptime(data_inizio, '%Y-%m-%d').date()
    if isinstance(data_fine, str):
        data_fine = datetime.strptime(data_fine, '%Y-%m-%d').date()

    curr = data_inizio
    mesi_coinvolti = set()
    while curr <= data_fine:
        mesi_coinvolti.add((curr.year, curr.month))
        if curr.month == 12:
            curr = date(curr.year + 1, 1, 1)
        else:
            curr = date(curr.year, curr.month + 1, 1)

    for anno, mese in mesi_coinvolti:
        num_days = cal_mod.monthrange(anno, mese)[1]
        primo = date(anno, mese, 1)
        ultimo = date(anno, mese, num_days)

        # 1) SINCRONIZZAZIONE OSTIA
        cur.execute("""
            SELECT COUNT(*) AS c 
            FROM turni_assegnazioni 
            WHERE data >= %s AND data <= %s AND COALESCE(nome, '') != ''
        """, (primo, ultimo))
        row = cur.fetchone()
        count = row['c'] if row and row.get('c') is not None else 0
        if count > 0:
            cur.execute("SELECT martedi_aperto, barbara_inclusa FROM turni_config WHERE anno=%s AND mese=%s", (anno, mese))
            cfg = cur.fetchone()
            marti = bool(cfg['martedi_aperto']) if cfg else False
            barb = bool(cfg['barbara_inclusa']) if cfg else False

            cur.execute("""
                SELECT nome, data_inizio, data_fine, tipo_evento, orario_richiesto 
                FROM piano_ferie 
                WHERE LOWER(TRIM(sede)) = 'ostia' 
                  AND (confermato = TRUE OR tipo_evento = 'malattia')
                  AND data_inizio <= %s 
                  AND data_fine >= %s
            """, (ultimo, primo))
            
            ferie_by_day = {}
            orari_richiesti_by_day = {}
            for r in cur.fetchall():
                fnome_raw = (r['nome'] or '').strip()
                fnome = fnome_raw.lower()
                if not fnome:
                    continue
                tipo_ev = r.get('tipo_evento') or 'ferie'
                orario_req = (r.get('orario_richiesto') or '').strip()
                d_start = max(r['data_inizio'], primo)
                d_end   = min(r['data_fine'], ultimo)
                d_curr = d_start
                while d_curr <= d_end:
                    c_iso = d_curr.isoformat()
                    if tipo_ev == 'giorno_libero' and orario_req:
                        if c_iso not in orari_richiesti_by_day:
                            orari_richiesti_by_day[c_iso] = []
                        orari_richiesti_by_day[c_iso].append({'nome': fnome_raw.title(), 'turno_id': orario_req})
                    else:
                        if c_iso not in ferie_by_day:
                            ferie_by_day[c_iso] = set()
                        ferie_by_day[c_iso].add(fnome)
                    d_curr += timedelta(days=1)

            # Raccogli dipendenti che hanno già un turno extra a Ostia
            extra_ostia_by_day = {}
            cur.execute("""
                SELECT data::text, nome 
                FROM turni_assegnazioni 
                WHERE data >= %s AND data <= %s 
                  AND turno_id LIKE 'extra_%%' 
                  AND COALESCE(nome, '') != ''
            """, (primo, ultimo))
            for r in cur.fetchall():
                c_iso = r['data']
                fnome = (r['nome'] or '').strip().lower()
                if fnome:
                    if c_iso not in extra_ostia_by_day:
                        extra_ostia_by_day[c_iso] = set()
                    extra_ostia_by_day[c_iso].add(fnome)

            # Chi ha già un turno extra o è in ferie/malattia NON è disponibile per turni ordinari
            unavail_by_day = {}
            for d_k in set(ferie_by_day.keys()) | set(extra_ostia_by_day.keys()):
                unavail_by_day[d_k] = set(ferie_by_day.get(d_k, set())) | set(extra_ostia_by_day.get(d_k, set()))

            schedule = _auto_genera_turni(anno, mese, marti, ferie_by_day=unavail_by_day, barbara_inclusa=barb)

            for d_str, reqs in orari_richiesti_by_day.items():
                if d_str in schedule and schedule[d_str].get('tipo') != 'chiuso':
                    day_ass = schedule[d_str].get('assegnazioni', {})
                    for req in reqs:
                        p_name = req['nome']
                        req_slot = req['turno_id']
                        target_slot = req_slot
                        if target_slot not in day_ass:
                            for k in day_ass.keys():
                                if k.startswith(req_slot):
                                    target_slot = k
                                    break
                        for sid, sname in list(day_ass.items()):
                            s_clean = (sname or '').strip().lower()
                            p_clean = (p_name or '').strip().lower()
                            if sid != target_slot and (s_clean == p_clean or s_clean == p_clean.split()[0] or p_clean == s_clean.split()[0]):
                                day_ass[sid] = ''
                        displaced = day_ass.get(target_slot, '')
                        day_ass[target_slot] = p_name
                        if displaced and displaced.lower() != p_name.lower():
                            for sid, sname in day_ass.items():
                                if not sname:
                                    day_ass[sid] = displaced
                                    break

            sub_start = max(data_inizio, primo)
            sub_end   = min(data_fine, ultimo)
            d_ptr = sub_start
            while d_ptr <= sub_end:
                d_str = d_ptr.isoformat()
                if d_str in schedule:
                    dd = schedule[d_str]
                    if dd.get('tipo') == 'chiuso':
                        cur.execute("UPDATE turni_assegnazioni SET nome = '', updated_at = NOW() WHERE data = %s", (d_ptr,))
                    else:
                        # Rimuovi preventivamente SOLO chi è assente da piano ferie (ferie/malattia)
                        assenti_oggi = ferie_by_day.get(d_str, set())
                        for ass_nome in assenti_oggi:
                            an_tokens = [t for t in ass_nome.split() if len(t) > 2] or [ass_nome]
                            cur.execute("""
                                UPDATE turni_assegnazioni 
                                SET nome = '', updated_at = NOW() 
                                WHERE data = %s AND (LOWER(TRIM(nome)) = LOWER(%s) OR LOWER(TRIM(nome)) = ANY(%s)) AND NOT turno_id LIKE 'extra_%%'
                            """, (d_ptr, ass_nome, an_tokens))
                            cur.execute("""
                                DELETE FROM turni_assegnazioni 
                                WHERE data = %s AND (LOWER(TRIM(nome)) = LOWER(%s) OR LOWER(TRIM(nome)) = ANY(%s)) AND turno_id LIKE 'extra_%%'
                            """, (d_ptr, ass_nome, an_tokens))

                        # Chi ha già un turno extra NON può ricevere un turno standard duplicato
                        extra_oggi = extra_ostia_by_day.get(d_str, set())
                        for tid, nome in dd.get('assegnazioni', {}).items():
                            nome_clean = (nome or '').strip()
                            nome_k = nome_clean.lower().split()[0] if nome_clean else ''
                            if nome_k in extra_oggi or (nome_clean.lower() in extra_oggi):
                                continue
                            cur.execute("""
                                INSERT INTO turni_assegnazioni (data, turno_id, nome, updated_at)
                                VALUES (%s, %s, %s, NOW())
                                ON CONFLICT (data, turno_id) DO UPDATE SET nome = EXCLUDED.nome, updated_at = NOW()
                            """, (d_ptr, tid, nome_clean))

                        # Deduplicazione post-sync DB Ostia:
                        # 1. Se una persona ha un extra, svuota qualsiasi turno standard con lo stesso nome
                        cur.execute("""
                            UPDATE turni_assegnazioni std
                            SET nome = '', updated_at = NOW()
                            FROM turni_assegnazioni ext
                            WHERE std.data = %s
                              AND ext.data = %s
                              AND ext.turno_id LIKE 'extra_%%'
                              AND COALESCE(ext.nome, '') != ''
                              AND NOT std.turno_id LIKE 'extra_%%'
                              AND (
                                  LOWER(TRIM(std.nome)) = LOWER(TRIM(ext.nome))
                                  OR SPLIT_PART(LOWER(TRIM(std.nome)), ' ', 1) = SPLIT_PART(LOWER(TRIM(ext.nome)), ' ', 1)
                              )
                        """, (d_ptr, d_ptr))

                        # 2. Se ci sono duplicati tra slot standard dello stesso giorno, mantieni il primo
                        cur.execute("""
                            SELECT id, turno_id, nome 
                            FROM turni_assegnazioni 
                            WHERE data = %s AND COALESCE(nome, '') != '' AND NOT turno_id LIKE 'extra_%%'
                            ORDER BY id ASC
                        """, (d_ptr,))
                        std_seen = set()
                        for s_row in cur.fetchall():
                            s_name = (s_row['nome'] or '').strip().lower().split()[0]
                            if s_name in std_seen:
                                cur.execute("UPDATE turni_assegnazioni SET nome = '', updated_at = NOW() WHERE id = %s", (s_row['id'],))
                            else:
                                std_seen.add(s_name)
                d_ptr += timedelta(days=1)

        # 2) SINCRONIZZAZIONE PALOCCO
        cur.execute("""
            SELECT COUNT(*) AS c 
            FROM turni_palocco_assegnazioni 
            WHERE data >= %s AND data <= %s AND COALESCE(nome, '') != ''
        """, (primo, ultimo))
        row_p = cur.fetchone()
        count_p = row_p['c'] if row_p and row_p.get('c') is not None else 0
        if count_p > 0:
            cur.execute("SELECT martedi_aperto FROM turni_palocco_config WHERE anno=%s AND mese=%s", (anno, mese))
            cfg_p = cur.fetchone()
            marti_p   = bool(cfg_p['martedi_aperto'])   if cfg_p else False

            cur.execute("""
                SELECT nome, data_inizio, data_fine, tipo_evento, orario_richiesto 
                FROM piano_ferie 
                WHERE LOWER(TRIM(sede)) IN ('palocco', 'secondo negozio') 
                  AND (confermato = TRUE OR tipo_evento = 'malattia')
                  AND data_inizio <= %s 
                  AND data_fine >= %s
            """, (ultimo, primo))
            
            ferie_p_by_day = {}
            orari_richiesti_p_by_day = {}
            for r in cur.fetchall():
                fnome_raw = (r['nome'] or '').strip()
                fnome = fnome_raw.lower()
                if not fnome:
                    continue
                tipo_ev = r.get('tipo_evento') or 'ferie'
                orario_req = (r.get('orario_richiesto') or '').strip()
                d_start = max(r['data_inizio'], primo)
                d_end   = min(r['data_fine'], ultimo)
                d_curr = d_start
                while d_curr <= d_end:
                    c_iso = d_curr.isoformat()
                    if tipo_ev == 'giorno_libero' and orario_req:
                        if c_iso not in orari_richiesti_p_by_day:
                            orari_richiesti_p_by_day[c_iso] = []
                        orari_richiesti_p_by_day[c_iso].append({'nome': fnome_raw.title(), 'turno_id': orario_req})
                    else:
                        if c_iso not in ferie_p_by_day:
                            ferie_p_by_day[c_iso] = set()
                        ferie_p_by_day[c_iso].add(fnome)
                    d_curr += timedelta(days=1)

            # Raccogli dipendenti che hanno già un turno extra a Palocco
            extra_palocco_by_day = {}
            cur.execute("""
                SELECT data::text, nome 
                FROM turni_palocco_assegnazioni 
                WHERE data >= %s AND data <= %s 
                  AND turno_id LIKE 'extra_%%' 
                  AND COALESCE(nome, '') != ''
            """, (primo, ultimo))
            for r in cur.fetchall():
                c_iso = r['data']
                fnome = (r['nome'] or '').strip().lower()
                if fnome:
                    if c_iso not in extra_palocco_by_day:
                        extra_palocco_by_day[c_iso] = set()
                    extra_palocco_by_day[c_iso].add(fnome)

            # Chi ha già un turno extra o è in ferie/malattia NON è disponibile per turni ordinari a Palocco
            unavail_p_by_day = {}
            for d_k in set(ferie_p_by_day.keys()) | set(extra_palocco_by_day.keys()):
                unavail_p_by_day[d_k] = set(ferie_p_by_day.get(d_k, set())) | set(extra_palocco_by_day.get(d_k, set()))

            marisa_ostia_days = _get_marisa_ostia_days(cur, anno, mese, primo, ultimo)
            sched_p = _auto_genera_turni_palocco(anno, mese, marti_p, ferie_by_day=unavail_p_by_day, marisa_ostia_days=marisa_ostia_days)

            for d_str, reqs in orari_richiesti_p_by_day.items():
                if d_str in sched_p and sched_p[d_str].get('tipo') != 'chiuso':
                    day_ass = sched_p[d_str].get('assegnazioni', {})
                    for req in reqs:
                        p_name = req['nome']
                        req_slot = req['turno_id']
                        target_slot = req_slot
                        if target_slot not in day_ass:
                            for k in day_ass.keys():
                                if k.startswith(req_slot):
                                    target_slot = k
                                    break
                        for sid, sname in list(day_ass.items()):
                            s_clean = (sname or '').strip().lower()
                            p_clean = (p_name or '').strip().lower()
                            if sid != target_slot and (s_clean == p_clean or s_clean == p_clean.split()[0] or p_clean == s_clean.split()[0]):
                                day_ass[sid] = ''
                        displaced = day_ass.get(target_slot, '')
                        day_ass[target_slot] = p_name
                        if displaced and displaced.lower() != p_name.lower():
                            for sid, sname in day_ass.items():
                                if not sname:
                                    day_ass[sid] = displaced
                                    break

            sub_start = max(data_inizio, primo)
            sub_end   = min(data_fine, ultimo)
            d_ptr = sub_start
            while d_ptr <= sub_end:
                d_str = d_ptr.isoformat()
                if d_str in sched_p:
                    dd = sched_p[d_str]
                    if dd.get('tipo') == 'chiuso':
                        cur.execute("UPDATE turni_palocco_assegnazioni SET nome = '', updated_at = NOW() WHERE data = %s", (d_ptr,))
                    else:
                        # Rimuovi preventivamente SOLO chi è assente per ferie/malattia da piano ferie
                        assenti_p_oggi = ferie_p_by_day.get(d_str, set())
                        for ass_nome in assenti_p_oggi:
                            an_tokens = [t for t in ass_nome.split() if len(t) > 2] or [ass_nome]
                            cur.execute("""
                                UPDATE turni_palocco_assegnazioni 
                                SET nome = '', updated_at = NOW() 
                                WHERE data = %s AND (LOWER(TRIM(nome)) = LOWER(%s) OR LOWER(TRIM(nome)) = ANY(%s)) AND NOT turno_id LIKE 'extra_%%'
                            """, (d_ptr, ass_nome, an_tokens))
                            cur.execute("""
                                DELETE FROM turni_palocco_assegnazioni 
                                WHERE data = %s AND (LOWER(TRIM(nome)) = LOWER(%s) OR LOWER(TRIM(nome)) = ANY(%s)) AND turno_id LIKE 'extra_%%'
                            """, (d_ptr, ass_nome, an_tokens))

                        # Chi ha già un turno extra NON può ricevere un turno standard duplicato
                        extra_p_oggi = extra_palocco_by_day.get(d_str, set())
                        for tid, nome in dd.get('assegnazioni', {}).items():
                            nome_clean = (nome or '').strip()
                            nome_k = nome_clean.lower().split()[0] if nome_clean else ''
                            if nome_k in extra_p_oggi or (nome_clean.lower() in extra_p_oggi):
                                continue
                            cur.execute("""
                                INSERT INTO turni_palocco_assegnazioni (data, turno_id, nome, updated_at)
                                VALUES (%s, %s, %s, NOW())
                                ON CONFLICT (data, turno_id) DO UPDATE SET nome = EXCLUDED.nome, updated_at = NOW()
                            """, (d_ptr, tid, nome_clean))

                        # Deduplicazione post-sync DB Palocco:
                        # 1. Se una persona ha un turno extra, svuota qualsiasi turno standard con la stessa persona
                        cur.execute("""
                            UPDATE turni_palocco_assegnazioni std
                            SET nome = '', updated_at = NOW()
                            FROM turni_palocco_assegnazioni ext
                            WHERE std.data = %s
                              AND ext.data = %s
                              AND ext.turno_id LIKE 'extra_%%'
                              AND COALESCE(ext.nome, '') != ''
                              AND NOT std.turno_id LIKE 'extra_%%'
                              AND (
                                  LOWER(TRIM(std.nome)) = LOWER(TRIM(ext.nome))
                                  OR SPLIT_PART(LOWER(TRIM(std.nome)), ' ', 1) = SPLIT_PART(LOWER(TRIM(ext.nome)), ' ', 1)
                              )
                        """, (d_ptr, d_ptr))

                        # 2. Se ci sono duplicati tra slot standard dello stesso giorno, mantieni il primo
                        cur.execute("""
                            SELECT id, turno_id, nome 
                            FROM turni_palocco_assegnazioni 
                            WHERE data = %s AND COALESCE(nome, '') != '' AND NOT turno_id LIKE 'extra_%%'
                            ORDER BY id ASC
                        """, (d_ptr,))
                        std_p_seen = set()
                        for s_row in cur.fetchall():
                            s_name = (s_row['nome'] or '').strip().lower().split()[0]
                            if s_name in std_p_seen:
                                cur.execute("UPDATE turni_palocco_assegnazioni SET nome = '', updated_at = NOW() WHERE id = %s", (s_row['id'],))
                            else:
                                std_p_seen.add(s_name)
                d_ptr += timedelta(days=1)


@app.route('/turni-settimanali')
def turni_settimanali():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    from datetime import date, datetime
    import calendar as cal_mod
    anno = request.args.get('anno', datetime.now().year, type=int)
    mese = request.args.get('mese', datetime.now().month, type=int)
    conn = get_db_connection()
    cur  = conn.cursor()
    # Ensure tables
    # Ensure tables and columns
    cur.execute("""
        CREATE TABLE IF NOT EXISTS turni_config (
            anno INTEGER NOT NULL, mese INTEGER NOT NULL,
            martedi_aperto BOOLEAN DEFAULT FALSE, barbara_inclusa BOOLEAN DEFAULT FALSE,
            PRIMARY KEY (anno, mese))""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS turni_assegnazioni (
            id SERIAL PRIMARY KEY, data DATE NOT NULL, turno_id TEXT NOT NULL,
            nome TEXT DEFAULT '', label TEXT DEFAULT '', updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE (data, turno_id))""")
    cur.execute("ALTER TABLE turni_assegnazioni ADD COLUMN IF NOT EXISTS label TEXT DEFAULT '';")
    conn.commit()
    cur.execute("SELECT martedi_aperto, barbara_inclusa FROM turni_config WHERE anno=%s AND mese=%s", (anno, mese))
    cfg = cur.fetchone()
    martedi_aperto  = bool(cfg['martedi_aperto'])  if cfg else False
    barbara_inclusa = bool(cfg['barbara_inclusa']) if cfg else False
    num_days = cal_mod.monthrange(anno, mese)[1]
    primo  = date(anno, mese, 1)
    ultimo = date(anno, mese, num_days)

    # Pulizia automatica duplicati nel mese visualizzato:
    # Se un dipendente ha un turno extra, rimuovi qualsiasi turno standard duplicato dello stesso giorno
    cur.execute("""
        UPDATE turni_assegnazioni std
        SET nome = '', updated_at = NOW()
        FROM turni_assegnazioni ext
        WHERE std.data = ext.data
          AND ext.data >= %s AND ext.data <= %s
          AND ext.turno_id LIKE 'extra_%%'
          AND COALESCE(ext.nome, '') != ''
          AND NOT std.turno_id LIKE 'extra_%%'
          AND (
              LOWER(TRIM(std.nome)) = LOWER(TRIM(ext.nome))
              OR SPLIT_PART(LOWER(TRIM(std.nome)), ' ', 1) = SPLIT_PART(LOWER(TRIM(ext.nome)), ' ', 1)
          )
    """, (primo, ultimo))
    conn.commit()

    cur.execute("SELECT data::text, turno_id, nome, label FROM turni_assegnazioni WHERE data >= %s AND data <= %s", (primo, ultimo))
    ass_map = {}
    for row in cur.fetchall():
        ds = row['data']
        if ds not in ass_map: ass_map[ds] = {}
        ass_map[ds][row['turno_id']] = {'nome': row['nome'] or '', 'label': row['label'] or ''}

    cur.close(); conn.close()
    ha_turni_generati = any(bool(info.get('nome')) for day in ass_map.values() for info in day.values())
    prev_mese = mese - 1 if mese > 1 else 12
    prev_anno = anno if mese > 1 else anno - 1
    next_mese = mese + 1 if mese < 12 else 1
    next_anno = anno if mese < 12 else anno + 1
    import calendar as _cal
    cal_grid = []
    for week in _cal.monthcalendar(anno, mese):
        week_data = []
        for dow, day_num in enumerate(week):
            if day_num == 0:
                week_data.append(None)
                continue
            d     = date(anno, mese, day_num)
            d_str = d.isoformat()
            is_tue = (dow == 1)
            if is_tue and not martedi_aperto:
                tipo  = 'chiuso'
                slots = []
            else:
                tipo  = _get_tipo_giorno_turni(dow)
                slots = TURNI_DEF[tipo]
            day_ass = ass_map.get(d_str, {})
            turni_giorno = []
            standard_ids = set(slot['id'] for slot in slots)
            seen_today = set()

            # 1. Raccogli prima i veri turni extra (hanno priorità sulle impostazioni predefinite)
            extra_items = []
            for tid, info in day_ass.items():
                if str(tid).startswith('extra_') and info.get('nome', '').strip():
                    nome = info['nome'].strip()
                    n_key = nome.lower().split()[0]
                    if n_key not in seen_today:
                        seen_today.add(n_key)
                        colore = TURNI_STAFF_COLORS.get(nome, '')
                        lbl = info.get('label') or 'Turno Extra'
                        extra_items.append({'id': tid, 'label': lbl, 'nome': nome, 'colore': colore, 'is_extra': True})

            # 2. Poi gli slot standard: salta se la persona è già presente (in extra o in un altro slot)
            for slot in slots:
                info = day_ass.get(slot['id'], {'nome': '', 'label': ''})
                nome = info.get('nome', '').strip()
                if nome:
                    n_key = nome.lower().split()[0]
                    if n_key not in seen_today:
                        seen_today.add(n_key)
                        colore = TURNI_STAFF_COLORS.get(nome, '')
                        turni_giorno.append({'id': slot['id'], 'label': slot['label'], 'nome': nome, 'colore': colore, 'is_extra': False})

            # 3. Aggiungi gli extra alla lista del giorno
            turni_giorno.extend(extra_items)

            week_data.append({
                'numero': day_num, 'data': d_str, 'tipo': tipo,
                'dow': dow, 'is_today': d == date.today(),
                'is_tuesday': is_tue, 'turni': turni_giorno,
                'has_data': bool(day_ass),
            })
        cal_grid.append(week_data)
    curr_username = (session.get('username') or '').strip().lower()
    can_reset_turni = curr_username in ['emanuele', 'roberto']
    ha_turni_generati = any(bool(info.get('nome')) for day in ass_map.values() for info in day.values())
    return render_template('turni_settimanali.html',
        anno=anno, mese=mese, mese_nome=MESI_IT_TURNI[mese],
        prev_anno=prev_anno, prev_mese=prev_mese,
        next_anno=next_anno, next_mese=next_mese,
        martedi_aperto=martedi_aperto, barbara_inclusa=barbara_inclusa,
        cal_grid=cal_grid, staff_nomi=TURNI_NOMI, staff_colors=TURNI_STAFF_COLORS,
        ha_turni_generati=ha_turni_generati,
        can_reset_turni=can_reset_turni)


@app.route('/turni-settimanali/genera', methods=['POST'])
def turni_genera():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    curr_username = (session.get('username') or '').strip().lower()
    if curr_username not in ['emanuele', 'roberto']:
        return jsonify({'error': 'Non autorizzato. Solo Roberto ed Emanuele possono generare i turni.'}), 403
    from datetime import date
    import calendar as cal_mod
    data = request.get_json()
    anno  = int(data.get('anno'))
    mese  = int(data.get('mese'))
    marti = bool(data.get('martedi_aperto', False))
    conn = get_db_connection()
    cur  = conn.cursor()
    # Leggi il valore barbara_inclusa salvato nel DB (non sovrascriverlo se non inviato dal client)
    cur.execute("SELECT barbara_inclusa FROM turni_config WHERE anno=%s AND mese=%s", (anno, mese))
    existing_cfg = cur.fetchone()
    barb_from_json = data.get('barbara_inclusa')
    if barb_from_json is not None:
        barb = bool(barb_from_json)
    elif existing_cfg:
        barb = bool(existing_cfg['barbara_inclusa'])
    else:
        barb = False
    cur.execute("""INSERT INTO turni_config (anno, mese, martedi_aperto, barbara_inclusa)
        VALUES (%s,%s,%s,%s) ON CONFLICT (anno, mese) DO UPDATE SET
        martedi_aperto=EXCLUDED.martedi_aperto, barbara_inclusa=EXCLUDED.barbara_inclusa""",
        (anno, mese, marti, barb))
    num_days = cal_mod.monthrange(anno, mese)[1]
    primo    = date(anno, mese, 1)
    ultimo   = date(anno, mese, num_days)

    # Ferie e richieste orari Ostia per questo mese
    cur.execute("""
        SELECT nome, data_inizio, data_fine, tipo_evento, orario_richiesto 
        FROM piano_ferie 
        WHERE LOWER(TRIM(sede)) = 'ostia' 
          AND (confermato = TRUE OR tipo_evento = 'malattia')
          AND data_inizio <= %s 
          AND data_fine >= %s
    """, (ultimo, primo))
    from datetime import timedelta
    ferie_by_day = {}
    orari_richiesti_by_day = {}
    for row in cur.fetchall():
        fnome_raw = (row['nome'] or '').strip()
        fnome = fnome_raw.lower()
        if not fnome:
            continue
        tipo_ev = row.get('tipo_evento') or 'ferie'
        orario_req = (row.get('orario_richiesto') or '').strip()
        d_start = max(row['data_inizio'], primo)
        d_end   = min(row['data_fine'], ultimo)
        curr = d_start
        while curr <= d_end:
            c_iso = curr.isoformat()
            if tipo_ev == 'giorno_libero' and orario_req:
                if c_iso not in orari_richiesti_by_day:
                    orari_richiesti_by_day[c_iso] = []
                orari_richiesti_by_day[c_iso].append({'nome': fnome_raw.title(), 'turno_id': orario_req})
            else:
                if c_iso not in ferie_by_day:
                    ferie_by_day[c_iso] = set()
                ferie_by_day[c_iso].add(fnome)
            curr += timedelta(days=1)

    schedule = _auto_genera_turni(anno, mese, marti, ferie_by_day=ferie_by_day, barbara_inclusa=barb)

    # Applica i turni con orario specifico richiesti da piano ferie
    for d_str, reqs in orari_richiesti_by_day.items():
        if d_str in schedule and schedule[d_str].get('tipo') != 'chiuso':
            day_ass = schedule[d_str].get('assegnazioni', {})
            for req in reqs:
                p_name = req['nome']
                req_slot = req['turno_id']
                
                # Risolvi lo slot target se non corrisponde esattamente all'id
                target_slot = req_slot
                if target_slot not in day_ass:
                    for k in day_ass.keys():
                        if k.startswith(req_slot):
                            target_slot = k
                            break

                # Rimuovi la persona da eventuali altri slot del giorno
                for sid, sname in list(day_ass.items()):
                    s_clean = (sname or '').strip().lower()
                    p_clean = (p_name or '').strip().lower()
                    if sid != target_slot and (s_clean == p_clean or s_clean == p_clean.split()[0] or p_clean == s_clean.split()[0]):
                        day_ass[sid] = ''

                displaced = day_ass.get(target_slot, '')
                day_ass[target_slot] = p_name

                # Se abbiamo rimpiazzato qualcuno, spostalo in uno slot vuoto dello stesso giorno
                if displaced and displaced.lower() != p_name.lower():
                    for sid, sname in day_ass.items():
                        if not sname:
                            day_ass[sid] = displaced
                            break

    cur.execute("DELETE FROM turni_assegnazioni WHERE data >= %s AND data <= %s", (primo, ultimo))
    for d_str, dd in schedule.items():
        for tid, nome in dd.get('assegnazioni', {}).items():
            cur.execute("""INSERT INTO turni_assegnazioni (data, turno_id, nome)
                VALUES (%s,%s,%s) ON CONFLICT (data, turno_id) DO UPDATE SET nome=EXCLUDED.nome""",
                (d_str, tid, nome or ''))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'ok': True})


@app.route('/turni-settimanali/reset', methods=['POST'])
def turni_reset():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    curr_username = (session.get('username') or '').strip().lower()
    if curr_username not in ['emanuele', 'roberto']:
        return jsonify({'error': 'Non autorizzato. Solo Roberto ed Emanuele possono resettare i turni.'}), 403
    from datetime import date
    import calendar as cal_mod
    data  = request.get_json()
    anno  = int(data.get('anno'))
    mese  = int(data.get('mese'))
    num_days = cal_mod.monthrange(anno, mese)[1]
    primo    = date(anno, mese, 1)
    ultimo   = date(anno, mese, num_days)
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("DELETE FROM turni_assegnazioni WHERE data >= %s AND data <= %s", (primo, ultimo))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True})


@app.route('/turni-settimanali/aggiorna-slot', methods=['POST'])
def turni_aggiorna_slot():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    data  = request.get_json()
    d_str = data.get('data')
    tid   = data.get('turno_id')
    nome  = (data.get('nome') or '').strip()
    label = data.get('label', '')
    conn  = get_db_connection()
    cur   = conn.cursor()
    if nome:
        cur.execute("""
            UPDATE turni_assegnazioni 
            SET nome = '', updated_at = NOW() 
            WHERE data = %s AND turno_id != %s 
              AND (LOWER(TRIM(nome)) = LOWER(TRIM(%s)) 
                   OR SPLIT_PART(LOWER(TRIM(nome)), ' ', 1) = SPLIT_PART(LOWER(TRIM(%s)), ' ', 1)
                   OR LOWER(TRIM(nome)) LIKE LOWER(TRIM(%s)) || '%%'
                   OR LOWER(TRIM(%s)) LIKE LOWER(TRIM(nome)) || '%%')
              AND NOT turno_id LIKE 'extra_%%'
        """, (d_str, tid, nome, nome, nome, nome))
        cur.execute("""
            DELETE FROM turni_assegnazioni 
            WHERE data = %s AND turno_id != %s 
              AND (LOWER(TRIM(nome)) = LOWER(TRIM(%s)) 
                   OR SPLIT_PART(LOWER(TRIM(nome)), ' ', 1) = SPLIT_PART(LOWER(TRIM(%s)), ' ', 1)
                   OR LOWER(TRIM(nome)) LIKE LOWER(TRIM(%s)) || '%%'
                   OR LOWER(TRIM(%s)) LIKE LOWER(TRIM(nome)) || '%%')
              AND turno_id LIKE 'extra_%%'
        """, (d_str, tid, nome, nome, nome, nome))
        if nome.lower() == 'marisa':
            # Se Marisa si trova ad Ostia, non può essere in turno a Palocco
            cur.execute("""
                UPDATE turni_palocco_assegnazioni 
                SET nome = '', updated_at = NOW() 
                WHERE data = %s AND LOWER(nome) = 'marisa' AND NOT turno_id LIKE 'extra_%%'
            """, (d_str,))
            cur.execute("""
                DELETE FROM turni_palocco_assegnazioni 
                WHERE data = %s AND LOWER(nome) = 'marisa' AND turno_id LIKE 'extra_%%'
            """, (d_str,))

    if label:
        cur.execute("""INSERT INTO turni_assegnazioni (data, turno_id, nome, label, updated_at)
            VALUES (%s,%s,%s,%s, NOW()) ON CONFLICT (data, turno_id)
            DO UPDATE SET nome=EXCLUDED.nome, label=EXCLUDED.label, updated_at=NOW()""", (d_str, tid, nome, label))
    else:
        cur.execute("""INSERT INTO turni_assegnazioni (data, turno_id, nome, updated_at)
            VALUES (%s,%s,%s, NOW()) ON CONFLICT (data, turno_id)
            DO UPDATE SET nome=EXCLUDED.nome, updated_at=NOW()""", (d_str, tid, nome))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'ok': True})


@app.route('/turni-settimanali/aggiungi-slot', methods=['POST'])
def turni_aggiungi_slot():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    import time
    data  = request.get_json()
    d_str = data.get('data')
    label = (data.get('label') or 'Turno Extra').strip()
    nome  = (data.get('nome') or '').strip()
    if not d_str or not nome:
        return jsonify({'error': 'Dati mancanti'}), 400

    conn = get_db_connection()
    cur  = conn.cursor()
    # Rimuovi la persona dal vecchio turno nello stesso giorno (elimina preimpostato se presente)
    cur.execute("""
        UPDATE turni_assegnazioni 
        SET nome = '', updated_at = NOW() 
        WHERE data = %s 
          AND (LOWER(TRIM(nome)) = LOWER(TRIM(%s)) 
               OR SPLIT_PART(LOWER(TRIM(nome)), ' ', 1) = SPLIT_PART(LOWER(TRIM(%s)), ' ', 1)
               OR LOWER(TRIM(nome)) LIKE LOWER(TRIM(%s)) || '%%'
               OR LOWER(TRIM(%s)) LIKE LOWER(TRIM(nome)) || '%%')
          AND NOT turno_id LIKE 'extra_%%'
    """, (d_str, nome, nome, nome, nome))
    cur.execute("""
        DELETE FROM turni_assegnazioni 
        WHERE data = %s 
          AND (LOWER(TRIM(nome)) = LOWER(TRIM(%s)) 
               OR SPLIT_PART(LOWER(TRIM(nome)), ' ', 1) = SPLIT_PART(LOWER(TRIM(%s)), ' ', 1)
               OR LOWER(TRIM(nome)) LIKE LOWER(TRIM(%s)) || '%%'
               OR LOWER(TRIM(%s)) LIKE LOWER(TRIM(nome)) || '%%')
          AND turno_id LIKE 'extra_%%'
    """, (d_str, nome, nome, nome, nome))
    if nome.lower() == 'marisa':
        # Se Marisa si trova ad Ostia, non può essere in turno a Palocco
        cur.execute("""
            UPDATE turni_palocco_assegnazioni 
            SET nome = '', updated_at = NOW() 
            WHERE data = %s AND LOWER(nome) = 'marisa' AND NOT turno_id LIKE 'extra_%%'
        """, (d_str,))
        cur.execute("""
            DELETE FROM turni_palocco_assegnazioni 
            WHERE data = %s AND LOWER(nome) = 'marisa' AND turno_id LIKE 'extra_%%'
        """, (d_str,))

    tid = f"extra_{int(time.time()*1000)}"
    cur.execute("""INSERT INTO turni_assegnazioni (data, turno_id, nome, label, updated_at)
        VALUES (%s, %s, %s, %s, NOW())""", (d_str, tid, nome, label))
    conn.commit()
    cur.close()
    conn.close()
    colore = TURNI_STAFF_COLORS.get(nome, '')
    return jsonify({'ok': True, 'turno_id': tid, 'label': label, 'nome': nome, 'colore': colore})


@app.route('/turni-settimanali/elimina-slot', methods=['POST'])
def turni_elimina_slot():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    data  = request.get_json()
    d_str = data.get('data')
    tid   = data.get('turno_id')
    conn  = get_db_connection()
    cur   = conn.cursor()
    if tid and str(tid).startswith('extra_'):
        cur.execute("DELETE FROM turni_assegnazioni WHERE data = %s AND turno_id = %s", (d_str, tid))
    else:
        cur.execute("""
            INSERT INTO turni_assegnazioni (data, turno_id, nome, updated_at)
            VALUES (%s, %s, '', NOW())
            ON CONFLICT (data, turno_id) DO UPDATE SET nome = '', updated_at = NOW()
        """, (d_str, tid))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True})


@app.route('/turni-settimanali/config', methods=['POST'])
def turni_config_route():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    curr_username = (session.get('username') or '').strip().lower()
    if curr_username not in ['emanuele', 'roberto']:
        return jsonify({'error': 'Non autorizzato. Solo Roberto ed Emanuele possono modificare la configurazione dei turni.'}), 403
    data  = request.get_json()
    anno  = int(data.get('anno'))
    mese  = int(data.get('mese'))
    marti = bool(data.get('martedi_aperto', False))
    barb  = bool(data.get('barbara_inclusa', False))
    conn  = get_db_connection()
    cur   = conn.cursor()
    cur.execute("""INSERT INTO turni_config (anno, mese, martedi_aperto, barbara_inclusa)
        VALUES (%s,%s,%s,%s) ON CONFLICT (anno, mese) DO UPDATE SET
        martedi_aperto=EXCLUDED.martedi_aperto, barbara_inclusa=EXCLUDED.barbara_inclusa""",
        (anno, mese, marti, barb))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'ok': True})


# ==============================================================
# TURNI PALOCCO
# ==============================================================

@app.route('/turni-palocco')
def turni_palocco():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    from datetime import date, datetime
    import calendar as cal_mod
    anno = request.args.get('anno', datetime.now().year, type=int)
    mese = request.args.get('mese', datetime.now().month, type=int)
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS turni_palocco_config (
            anno INTEGER NOT NULL, mese INTEGER NOT NULL,
            martedi_aperto BOOLEAN DEFAULT FALSE,
            PRIMARY KEY (anno, mese))""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS turni_palocco_assegnazioni (
            id SERIAL PRIMARY KEY, data DATE NOT NULL, turno_id TEXT NOT NULL,
            nome TEXT DEFAULT '', label TEXT DEFAULT '', updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE (data, turno_id))""")
    cur.execute("ALTER TABLE turni_palocco_assegnazioni ADD COLUMN IF NOT EXISTS label TEXT DEFAULT '';")
    conn.commit()
    cur.execute("SELECT martedi_aperto FROM turni_palocco_config WHERE anno=%s AND mese=%s", (anno, mese))
    cfg = cur.fetchone()
    martedi_aperto = bool(cfg['martedi_aperto']) if cfg else False
    num_days = cal_mod.monthrange(anno, mese)[1]
    primo  = date(anno, mese, 1)
    ultimo = date(anno, mese, num_days)

    # Pulizia automatica duplicati nel mese visualizzato:
    # Se un dipendente ha un turno extra, rimuovi qualsiasi turno standard duplicato dello stesso giorno
    cur.execute("""
        UPDATE turni_palocco_assegnazioni std
        SET nome = '', updated_at = NOW()
        FROM turni_palocco_assegnazioni ext
        WHERE std.data = ext.data
          AND ext.data >= %s AND ext.data <= %s
          AND ext.turno_id LIKE 'extra_%%'
          AND COALESCE(ext.nome, '') != ''
          AND NOT std.turno_id LIKE 'extra_%%'
          AND (
              LOWER(TRIM(std.nome)) = LOWER(TRIM(ext.nome))
              OR SPLIT_PART(LOWER(TRIM(std.nome)), ' ', 1) = SPLIT_PART(LOWER(TRIM(ext.nome)), ' ', 1)
          )
    """, (primo, ultimo))

    # Pulizia vecchi slot standard di sabato ('7-14', '14-20:30') non più validi dopo introduzione -a e -b
    cur.execute("""
        DELETE FROM turni_palocco_assegnazioni
        WHERE data >= %s AND data <= %s 
          AND EXTRACT(DOW FROM data) = 6 
          AND turno_id IN ('7-14', '14-20:30')
    """, (primo, ultimo))

    # Regola fondamentale: Marisa non deve MAI essere in turno di sabato o domenica se Francesca (o altro collega del team) è libera/disponibile
    cur.execute("""
        SELECT data::text, turno_id 
        FROM turni_palocco_assegnazioni 
        WHERE data >= %s AND data <= %s 
          AND EXTRACT(DOW FROM data) IN (0, 6) 
          AND LOWER(TRIM(nome)) = 'marisa' 
          AND NOT turno_id LIKE 'extra_%%'
    """, (primo, ultimo))
    m_rows = cur.fetchall()
    for m_r in m_rows:
        m_data = m_r['data']
        m_tid = m_r['turno_id']
        cur.execute("""
            SELECT nome FROM turni_palocco_assegnazioni 
            WHERE data = %s AND COALESCE(nome, '') != ''
        """, (m_data,))
        already_on = set((r['nome'] or '').strip().lower().split()[0] for r in cur.fetchall())
        for cand in ['Francesca', 'Patrizia', 'Ivano', 'Mariapia', 'Pietro']:
            cand_k = cand.lower()
            if cand_k not in already_on:
                cur.execute("""
                    SELECT COUNT(*) AS c FROM piano_ferie 
                    WHERE LOWER(TRIM(sede)) IN ('palocco', 'secondo negozio') 
                      AND (confermato = TRUE OR tipo_evento = 'malattia')
                      AND data_inizio <= %s AND data_fine >= %s
                      AND (LOWER(TRIM(nome)) = %s OR LOWER(TRIM(nome)) LIKE %s || '%%')
                """, (m_data, m_data, cand_k, cand_k))
                pf_row = cur.fetchone()
                if not pf_row or pf_row['c'] == 0:
                    cur.execute("""
                        UPDATE turni_palocco_assegnazioni 
                        SET nome = %s, updated_at = NOW() 
                        WHERE data = %s AND turno_id = %s
                    """, (cand, m_data, m_tid))
                    break
    conn.commit()

    cur.execute("SELECT data::text, turno_id, nome, label FROM turni_palocco_assegnazioni WHERE data >= %s AND data <= %s", (primo, ultimo))
    ass_map = {}
    for row in cur.fetchall():
        ds = row['data']
        if ds not in ass_map: ass_map[ds] = {}
        ass_map[ds][row['turno_id']] = {'nome': row['nome'] or '', 'label': row['label'] or ''}

    cur.close(); conn.close()
    ha_turni_generati = any(bool(info.get('nome')) for day in ass_map.values() for info in day.values())
    prev_mese = mese - 1 if mese > 1 else 12
    prev_anno = anno if mese > 1 else anno - 1
    next_mese = mese + 1 if mese < 12 else 1
    next_anno = anno if mese < 12 else anno + 1
    import calendar as _cal
    cal_grid = []
    for week in _cal.monthcalendar(anno, mese):
        week_data = []
        for dow, day_num in enumerate(week):
            if day_num == 0:
                week_data.append(None)
                continue
            d     = date(anno, mese, day_num)
            d_str = d.isoformat()
            is_tue = (dow == 1)
            if is_tue and not martedi_aperto:
                tipo  = 'chiuso'
                slots = []
            else:
                tipo  = _get_tipo_giorno_turni(dow)
                slots = TURNI_PALOCCO_DEF[tipo]
            day_ass = ass_map.get(d_str, {})
            turni_giorno = []
            standard_ids = set(slot['id'] for slot in slots)
            seen_today = set()

            # 1. Raccogli prima i veri turni extra (hanno priorità sulle impostazioni predefinite)
            extra_items = []
            for tid, info in day_ass.items():
                if str(tid).startswith('extra_') and info.get('nome', '').strip():
                    nome = info['nome'].strip()
                    n_key = nome.lower().split()[0]
                    if n_key not in seen_today:
                        seen_today.add(n_key)
                        colore = TURNI_PALOCCO_STAFF_COLORS.get(nome, '')
                        lbl = info.get('label') or 'Turno Extra'
                        extra_items.append({'id': tid, 'label': lbl, 'nome': nome, 'colore': colore, 'is_extra': True})

            # 2. Poi gli slot standard: salta se la persona è già presente (in extra o in un altro slot)
            for slot in slots:
                info = day_ass.get(slot['id'], {'nome': '', 'label': ''})
                nome = info.get('nome', '').strip()
                if nome:
                    n_key = nome.lower().split()[0]
                    if n_key not in seen_today:
                        seen_today.add(n_key)
                        colore = TURNI_PALOCCO_STAFF_COLORS.get(nome, '')
                        turni_giorno.append({'id': slot['id'], 'label': slot['label'], 'nome': nome, 'colore': colore, 'is_extra': False})

            # 3. Aggiungi gli extra alla lista del giorno
            turni_giorno.extend(extra_items)

            week_data.append({
                'numero': day_num, 'data': d_str, 'tipo': tipo,
                'dow': dow, 'is_today': d == date.today(),
                'is_tuesday': is_tue, 'turni': turni_giorno,
                'has_data': bool(day_ass),
            })
        cal_grid.append(week_data)
    curr_username = (session.get('username') or '').strip().lower()
    can_reset_turni = curr_username in ['emanuele', 'roberto']
    ha_turni_generati = any(bool(info.get('nome')) for day in ass_map.values() for info in day.values())
    return render_template('turni_palocco.html',
        anno=anno, mese=mese, mese_nome=MESI_IT_TURNI[mese],
        prev_anno=prev_anno, prev_mese=prev_mese,
        next_anno=next_anno, next_mese=next_mese,
        martedi_aperto=martedi_aperto,
        cal_grid=cal_grid, staff_nomi=TURNI_PALOCCO_NOMI, staff_colors=TURNI_PALOCCO_STAFF_COLORS,
        ha_turni_generati=ha_turni_generati,
        can_reset_turni=can_reset_turni)


@app.route('/turni-palocco/genera', methods=['POST'])
def turni_palocco_genera():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    curr_username = (session.get('username') or '').strip().lower()
    if curr_username not in ['emanuele', 'roberto']:
        return jsonify({'error': 'Non autorizzato. Solo Roberto ed Emanuele possono generare i turni.'}), 403
    from datetime import date, timedelta
    import calendar as cal_mod
    data = request.get_json()
    anno  = int(data.get('anno'))
    mese  = int(data.get('mese'))
    marti = bool(data.get('martedi_aperto', False))
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""INSERT INTO turni_palocco_config (anno, mese, martedi_aperto)
        VALUES (%s,%s,%s) ON CONFLICT (anno, mese) DO UPDATE SET
        martedi_aperto=EXCLUDED.martedi_aperto""",
        (anno, mese, marti))
    num_days = cal_mod.monthrange(anno, mese)[1]
    primo    = date(anno, mese, 1)
    ultimo   = date(anno, mese, num_days)

    cur.execute("""
        SELECT nome, data_inizio, data_fine, tipo_evento, orario_richiesto 
        FROM piano_ferie 
        WHERE LOWER(TRIM(sede)) IN ('palocco', 'secondo negozio') 
          AND (confermato = TRUE OR tipo_evento = 'malattia')
          AND data_inizio <= %s 
          AND data_fine >= %s
    """, (ultimo, primo))
    ferie_by_day = {}
    orari_richiesti_by_day = {}
    for row in cur.fetchall():
        fnome_raw = (row['nome'] or '').strip()
        fnome = fnome_raw.lower()
        if not fnome:
            continue
        tipo_ev = row.get('tipo_evento') or 'ferie'
        orario_req = (row.get('orario_richiesto') or '').strip()
        d_start = max(row['data_inizio'], primo)
        d_end   = min(row['data_fine'], ultimo)
        curr = d_start
        while curr <= d_end:
            c_iso = curr.isoformat()
            if tipo_ev == 'giorno_libero' and orario_req:
                if c_iso not in orari_richiesti_by_day:
                    orari_richiesti_by_day[c_iso] = []
                orari_richiesti_by_day[c_iso].append({'nome': fnome_raw.title(), 'turno_id': orario_req})
            else:
                if c_iso not in ferie_by_day:
                    ferie_by_day[c_iso] = set()
                ferie_by_day[c_iso].add(fnome)
            curr += timedelta(days=1)

    # Recupera i giorni in cui Marisa è già in turno a Ostia (non può essere a Palocco)
    marisa_ostia_days = _get_marisa_ostia_days(cur, anno, mese, primo, ultimo)

    schedule = _auto_genera_turni_palocco(anno, mese, marti, ferie_by_day=ferie_by_day, marisa_ostia_days=marisa_ostia_days)

    for d_str, reqs in orari_richiesti_by_day.items():
        if d_str in schedule and schedule[d_str].get('tipo') != 'chiuso':
            day_ass = schedule[d_str].get('assegnazioni', {})
            for req in reqs:
                p_name = req['nome']
                req_slot = req['turno_id']
                target_slot = req_slot
                if target_slot not in day_ass:
                    for k in day_ass.keys():
                        if k.startswith(req_slot):
                            target_slot = k
                            break
                # Rimuovi la persona da eventuali altri slot del giorno
                for sid, sname in list(day_ass.items()):
                    s_clean = (sname or '').strip().lower()
                    p_clean = (p_name or '').strip().lower()
                    if sid != target_slot and (s_clean == p_clean or s_clean == p_clean.split()[0] or p_clean == s_clean.split()[0]):
                        day_ass[sid] = ''
                displaced = day_ass.get(target_slot, '')
                day_ass[target_slot] = p_name
                if displaced and displaced.lower() != p_name.lower():
                    for sid, sname in day_ass.items():
                        if not sname:
                            day_ass[sid] = displaced
                            break

    cur.execute("DELETE FROM turni_palocco_assegnazioni WHERE data >= %s AND data <= %s", (primo, ultimo))
    for d_str, dd in schedule.items():
        for tid, nome in dd.get('assegnazioni', {}).items():
            cur.execute("""INSERT INTO turni_palocco_assegnazioni (data, turno_id, nome)
                VALUES (%s,%s,%s) ON CONFLICT (data, turno_id) DO UPDATE SET nome=EXCLUDED.nome""",
                (d_str, tid, nome or ''))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'ok': True})


@app.route('/turni-palocco/reset', methods=['POST'])
def turni_palocco_reset():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    curr_username = (session.get('username') or '').strip().lower()
    if curr_username not in ['emanuele', 'roberto']:
        return jsonify({'error': 'Non autorizzato. Solo Roberto ed Emanuele possono resettare i turni.'}), 403
    from datetime import date
    import calendar as cal_mod
    data  = request.get_json()
    anno  = int(data.get('anno'))
    mese  = int(data.get('mese'))
    num_days = cal_mod.monthrange(anno, mese)[1]
    primo    = date(anno, mese, 1)
    ultimo   = date(anno, mese, num_days)
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("DELETE FROM turni_palocco_assegnazioni WHERE data >= %s AND data <= %s", (primo, ultimo))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True})


@app.route('/turni-palocco/aggiorna-slot', methods=['POST'])
def turni_palocco_aggiorna_slot():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    data  = request.get_json()
    d_str = data.get('data')
    tid   = data.get('turno_id')
    nome  = (data.get('nome') or '').strip()
    label = data.get('label', '')
    conn  = get_db_connection()
    cur   = conn.cursor()
    if nome:
        # Se si tenta di assegnare Marisa a Palocco, verificare che non sia già ad Ostia in questo giorno
        if nome.lower() == 'marisa':
            cur.execute("""
                SELECT turno_id, COALESCE(label, turno_id) AS lbl 
                FROM turni_assegnazioni 
                WHERE data = %s AND LOWER(nome) = 'marisa' AND COALESCE(nome, '') != ''
            """, (d_str,))
            row_ostia = cur.fetchone()
            if row_ostia:
                cur.close(); conn.close()
                return jsonify({
                    'ok': False,
                    'error': f"Marisa è già in turno ad Ostia il {d_str} ({row_ostia['lbl']}) e non può essere a Secondo negozio."
                }), 400

        cur.execute("""
            UPDATE turni_palocco_assegnazioni 
            SET nome = '', updated_at = NOW() 
            WHERE data = %s AND turno_id != %s 
              AND (LOWER(TRIM(nome)) = LOWER(TRIM(%s)) 
                   OR SPLIT_PART(LOWER(TRIM(nome)), ' ', 1) = SPLIT_PART(LOWER(TRIM(%s)), ' ', 1)
                   OR LOWER(TRIM(nome)) LIKE LOWER(TRIM(%s)) || '%%'
                   OR LOWER(TRIM(%s)) LIKE LOWER(TRIM(nome)) || '%%')
              AND NOT turno_id LIKE 'extra_%%'
        """, (d_str, tid, nome, nome, nome, nome))
        cur.execute("""
            DELETE FROM turni_palocco_assegnazioni 
            WHERE data = %s AND turno_id != %s 
              AND (LOWER(TRIM(nome)) = LOWER(TRIM(%s)) 
                   OR SPLIT_PART(LOWER(TRIM(nome)), ' ', 1) = SPLIT_PART(LOWER(TRIM(%s)), ' ', 1)
                   OR LOWER(TRIM(nome)) LIKE LOWER(TRIM(%s)) || '%%'
                   OR LOWER(TRIM(%s)) LIKE LOWER(TRIM(nome)) || '%%')
              AND turno_id LIKE 'extra_%%'
        """, (d_str, tid, nome, nome, nome, nome))

    if label:
        cur.execute("""INSERT INTO turni_palocco_assegnazioni (data, turno_id, nome, label, updated_at)
            VALUES (%s,%s,%s,%s, NOW()) ON CONFLICT (data, turno_id)
            DO UPDATE SET nome=EXCLUDED.nome, label=EXCLUDED.label, updated_at=NOW()""", (d_str, tid, nome, label))
    else:
        cur.execute("""INSERT INTO turni_palocco_assegnazioni (data, turno_id, nome, updated_at)
            VALUES (%s,%s,%s, NOW()) ON CONFLICT (data, turno_id)
            DO UPDATE SET nome=EXCLUDED.nome, updated_at=NOW()""", (d_str, tid, nome))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'ok': True})


@app.route('/turni-palocco/aggiungi-slot', methods=['POST'])
def turni_palocco_aggiungi_slot():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    import time
    data  = request.get_json()
    d_str = data.get('data')
    label = (data.get('label') or 'Turno Extra').strip()
    nome  = (data.get('nome') or '').strip()
    if not d_str or not nome:
        return jsonify({'error': 'Dati mancanti'}), 400

    conn = get_db_connection()
    cur  = conn.cursor()

    # Se si tenta di aggiungere Marisa a Palocco, verificare che non sia già ad Ostia in questo giorno
    if nome.lower() == 'marisa':
        cur.execute("""
            SELECT turno_id, COALESCE(label, turno_id) AS lbl 
            FROM turni_assegnazioni 
            WHERE data = %s AND LOWER(nome) = 'marisa' AND COALESCE(nome, '') != ''
        """, (d_str,))
        row_ostia = cur.fetchone()
        if row_ostia:
            cur.close(); conn.close()
            return jsonify({
                'ok': False,
                'error': f"Marisa è già in turno ad Ostia il {d_str} ({row_ostia['lbl']}) e non può essere a Secondo negozio."
            }), 400

    # Rimuovi la persona dal vecchio turno nello stesso giorno (elimina preimpostato se presente)
    cur.execute("""
        UPDATE turni_palocco_assegnazioni 
        SET nome = '', updated_at = NOW() 
        WHERE data = %s 
          AND (LOWER(TRIM(nome)) = LOWER(TRIM(%s)) 
               OR SPLIT_PART(LOWER(TRIM(nome)), ' ', 1) = SPLIT_PART(LOWER(TRIM(%s)), ' ', 1)
               OR LOWER(TRIM(nome)) LIKE LOWER(TRIM(%s)) || '%%'
               OR LOWER(TRIM(%s)) LIKE LOWER(TRIM(nome)) || '%%')
          AND NOT turno_id LIKE 'extra_%%'
    """, (d_str, nome, nome, nome, nome))
    cur.execute("""
        DELETE FROM turni_palocco_assegnazioni 
        WHERE data = %s 
          AND (LOWER(TRIM(nome)) = LOWER(TRIM(%s)) 
               OR SPLIT_PART(LOWER(TRIM(nome)), ' ', 1) = SPLIT_PART(LOWER(TRIM(%s)), ' ', 1)
               OR LOWER(TRIM(nome)) LIKE LOWER(TRIM(%s)) || '%%'
               OR LOWER(TRIM(%s)) LIKE LOWER(TRIM(nome)) || '%%')
          AND turno_id LIKE 'extra_%%'
    """, (d_str, nome, nome, nome, nome))

    tid = f"extra_{int(time.time()*1000)}"
    cur.execute("""INSERT INTO turni_palocco_assegnazioni (data, turno_id, nome, label, updated_at)
        VALUES (%s, %s, %s, %s, NOW())""", (d_str, tid, nome, label))
    conn.commit()
    cur.close()
    conn.close()
    colore = TURNI_PALOCCO_STAFF_COLORS.get(nome, '')
    return jsonify({'ok': True, 'turno_id': tid, 'label': label, 'nome': nome, 'colore': colore})


@app.route('/turni-palocco/elimina-slot', methods=['POST'])
def turni_palocco_elimina_slot():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    data  = request.get_json()
    d_str = data.get('data')
    tid   = data.get('turno_id')
    conn  = get_db_connection()
    cur   = conn.cursor()
    if tid and str(tid).startswith('extra_'):
        cur.execute("DELETE FROM turni_palocco_assegnazioni WHERE data = %s AND turno_id = %s", (d_str, tid))
    else:
        cur.execute("""
            INSERT INTO turni_palocco_assegnazioni (data, turno_id, nome, updated_at)
            VALUES (%s, %s, '', NOW())
            ON CONFLICT (data, turno_id) DO UPDATE SET nome = '', updated_at = NOW()
        """, (d_str, tid))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True})


@app.route('/turni-palocco/config', methods=['POST'])
def turni_palocco_config_route():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    curr_username = (session.get('username') or '').strip().lower()
    if curr_username not in ['emanuele', 'roberto']:
        return jsonify({'error': 'Non autorizzato. Solo Roberto ed Emanuele possono modificare la configurazione dei turni.'}), 403
    data  = request.get_json()
    anno  = int(data.get('anno'))
    mese  = int(data.get('mese'))
    marti = bool(data.get('martedi_aperto', False))
    conn  = get_db_connection()
    cur   = conn.cursor()
    cur.execute("""INSERT INTO turni_palocco_config (anno, mese, martedi_aperto)
        VALUES (%s,%s,%s) ON CONFLICT (anno, mese) DO UPDATE SET
        martedi_aperto=EXCLUDED.martedi_aperto""",
        (anno, mese, marti))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'ok': True})



# ==============================================================
# GESTIONE REGISTRO ALLERGENI
# ==============================================================

ALLERGENI_DEFINIZIONI = [
    {"id": "glutine", "nome": "Cereali con Glutine", "dettaglio": "Grano, segale, orzo, avena, farro, kamut", "icon": "fa-wheat-awn", "badge_color": "#d97706"},
    {"id": "crostacei", "nome": "Crostacei", "dettaglio": "Gamberi, scampi, aragoste, granchi", "icon": "fa-shrimp", "badge_color": "#ef4444"},
    {"id": "uova", "nome": "Uova", "dettaglio": "Uova e prodotti a base di uova", "icon": "fa-egg", "badge_color": "#eab308"},
    {"id": "pesce", "nome": "Pesce", "dettaglio": "Pesce e prodotti a base di pesce", "icon": "fa-fish", "badge_color": "#0284c7"},
    {"id": "arachidi", "nome": "Arachidi", "dettaglio": "Arachidi e prodotti a base di arachidi", "icon": "fa-cubes-stacked", "badge_color": "#b45309"},
    {"id": "soia", "nome": "Soia", "dettaglio": "Soia e prodotti a base di soia", "icon": "fa-seedling", "badge_color": "#16a34a"},
    {"id": "latte", "nome": "Latte e Lattosio", "dettaglio": "Latte, formaggi, burro, yogurt, panna", "icon": "fa-glass-water", "badge_color": "#2563eb"},
    {"id": "frutta_guscio", "nome": "Frutta a Guscio", "dettaglio": "Mandorle, nocciole, noci, pistacchi, anacardi", "icon": "fa-tree", "badge_color": "#78350f"},
    {"id": "sedano", "nome": "Sedano", "dettaglio": "Sedano e prodotti a base di sedano", "icon": "fa-leaf", "badge_color": "#65a30d"},
    {"id": "senape", "nome": "Senape", "dettaglio": "Senape e prodotti a base di senape", "icon": "fa-jar", "badge_color": "#ca8a04"},
    {"id": "sesamo", "nome": "Semi di Sesamo", "dettaglio": "Semi di sesamo e prodotti a base di sesamo", "icon": "fa-circle-dot", "badge_color": "#a16207"},
    {"id": "solfiti", "nome": "Solfiti", "dettaglio": "Anidride solforosa e solfiti > 10 mg/kg", "icon": "fa-wine-bottle", "badge_color": "#9333ea"},
    {"id": "lupini", "nome": "Lupini", "dettaglio": "Lupini e prodotti a base di lupini", "icon": "fa-spa", "badge_color": "#ca8a04"},
    {"id": "molluschi", "nome": "Molluschi", "dettaglio": "Polpi, seppie, calamari, cozze, vongole", "icon": "fa-water", "badge_color": "#0d9488"}
]

ALLERGENI_DICT = {a['id']: a for a in ALLERGENI_DEFINIZIONI}

@app.route('/allergeni')
def allergeni_index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS allergeni_prodotti (
                id SERIAL PRIMARY KEY,
                titolo TEXT NOT NULL,
                ingredienti TEXT DEFAULT '',
                allergeni JSONB DEFAULT '[]'::jsonb,
                tracce TEXT DEFAULT '',
                note TEXT DEFAULT '',
                creato_il TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                aggiornato_il TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        cur.execute("SELECT * FROM allergeni_prodotti ORDER BY LOWER(titolo) ASC")
        rows = cur.fetchall()
        cur.close()
        conn.close()

        prodotti = []
        for r in rows:
            p = dict(r)
            raw_all = p.get('allergeni')
            if isinstance(raw_all, str):
                try:
                    all_list = json.loads(raw_all)
                except Exception:
                    all_list = []
            elif isinstance(raw_all, list):
                all_list = raw_all
            else:
                all_list = []
            p['allergeni_list'] = all_list
            p['allergeni_dettagli'] = [ALLERGENI_DICT[aid] for aid in all_list if aid in ALLERGENI_DICT]
            p['allergeni_count'] = len(p['allergeni_dettagli'])
            prodotti.append(p)
        return render_template('allergeni.html', prodotti=prodotti, totale=len(prodotti))
    except Exception as e:
        print(f"Errore caricamento allergeni: {e}")
        flash("Errore nel caricamento del registro allergeni.", "danger")
        return render_template('allergeni.html', prodotti=[], totale=0)

@app.route('/allergeni/crea', methods=['POST'])
def allergeni_crea():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    titolo = (request.form.get('titolo') or '').strip()
    if not titolo:
        flash("Inserisci il nome del prodotto (es. Pan di spagna).", "warning")
        return redirect(url_for('allergeni_index'))
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO allergeni_prodotti (titolo, ingredienti, allergeni, tracce, note, creato_il, aggiornato_il)
            VALUES (%s, '', '[]'::jsonb, '', '', NOW(), NOW())
            RETURNING id;
        """, (titolo,))
        new_row = cur.fetchone()
        new_id = new_row['id']
        conn.commit()
        cur.close()
        conn.close()
        flash(f"Prodotto '{titolo}' creato! Ora puoi inserire gli allergeni e gli ingredienti.", "success")
        return redirect(url_for('allergeni_scheda', id=new_id))
    except Exception as e:
        print(f"Errore creazione allergene: {e}")
        flash("Errore durante la creazione del prodotto.", "danger")
        return redirect(url_for('allergeni_index'))

@app.route('/allergeni/scheda/<int:id>')
def allergeni_scheda(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM allergeni_prodotti WHERE id = %s", (id,))
        p = cur.fetchone()
        cur.close()
        conn.close()
        if not p:
            flash("Scheda prodotto non trovata.", "warning")
            return redirect(url_for('allergeni_index'))
        
        p = dict(p)
        raw_all = p.get('allergeni')
        if isinstance(raw_all, str):
            try:
                selected_ids = set(json.loads(raw_all))
            except Exception:
                selected_ids = set()
        elif isinstance(raw_all, list):
            selected_ids = set(raw_all)
        else:
            selected_ids = set()

        allergeni_view = []
        for a in ALLERGENI_DEFINIZIONI:
            item = dict(a)
            item['attivo'] = (a['id'] in selected_ids)
            allergeni_view.append(item)

        return render_template('allergeni_scheda.html', p=p, allergeni=allergeni_view, allergeni_def=ALLERGENI_DEFINIZIONI)
    except Exception as e:
        print(f"Errore lettura scheda allergene: {e}")
        flash("Errore nel caricamento della scheda.", "danger")
        return redirect(url_for('allergeni_index'))

@app.route('/allergeni/salva/<int:id>', methods=['POST'])
def allergeni_salva(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    titolo = (request.form.get('titolo') or '').strip()
    ingredienti = (request.form.get('ingredienti') or '').strip()
    tracce = (request.form.get('tracce') or '').strip()
    note = (request.form.get('note') or '').strip()
    allergeni_scelti = request.form.getlist('allergeni')

    if not titolo:
        flash("Il nome del prodotto non può essere vuoto.", "warning")
        return redirect(url_for('allergeni_scheda', id=id))

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE allergeni_prodotti 
            SET titolo = %s,
                ingredienti = %s,
                allergeni = %s,
                tracce = %s,
                note = %s,
                aggiornato_il = NOW()
            WHERE id = %s
        """, (titolo, ingredienti, Json(allergeni_scelti), tracce, note, id))
        conn.commit()
        cur.close()
        conn.close()
        flash(f"Scheda '{titolo}' salvata con successo!", "success")
        return redirect(url_for('allergeni_scheda', id=id))
    except Exception as e:
        print(f"Errore salvataggio scheda allergene: {e}")
        flash("Errore durante il salvataggio della scheda.", "danger")
        return redirect(url_for('allergeni_scheda', id=id))

@app.route('/allergeni/elimina/<int:id>', methods=['POST', 'GET'])
def allergeni_elimina(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT titolo FROM allergeni_prodotti WHERE id = %s", (id,))
        row = cur.fetchone()
        titolo = row['titolo'] if row else 'Prodotto'
        cur.execute("DELETE FROM allergeni_prodotti WHERE id = %s", (id,))
        conn.commit()
        cur.close()
        conn.close()
        flash(f"Scheda '{titolo}' eliminata con successo.", "success")
    except Exception as e:
        print(f"Errore eliminazione allergene: {e}")
        flash("Errore durante l'eliminazione della scheda.", "danger")
    return redirect(url_for('allergeni_index'))


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
