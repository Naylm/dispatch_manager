import eventlet
eventlet.monkey_patch()

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify, send_file
)
from flask_socketio import SocketIO, join_room, emit
import sqlite3
import os
from datetime import datetime, timedelta
import pandas as pd
import pdfkit
from io import BytesIO

app = Flask(__name__)
app.secret_key = "supersecret"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)

socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")

DB = "dispatch.db"

# 1) Chemin vers wkhtmltopdf.exe – à adapter si nécessaire
WKHTMLTOPDF_PATH = r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe"
pdf_config = pdfkit.configuration(wkhtmltopdf=WKHTMLTOPDF_PATH)


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


@app.template_filter("format_date")
def format_date(d):
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d-%m-%Y")
    except:
        return d


@app.before_request
def renew_session():
    session.permanent = True


# ---------- ROUTE : Accueil ----------
@app.route("/")
def home():
    if "user" not in session:
        return redirect(url_for("login"))

    db = get_db()
    if session["role"] == "admin":
        incidents = db.execute("SELECT * FROM incidents WHERE archived=0").fetchall()
        techniciens = db.execute("SELECT * FROM techniciens").fetchall()
    else:
        incidents = db.execute(
            "SELECT * FROM incidents WHERE LOWER(collaborateur)=LOWER(?) AND archived=0",
            (session["user"],),
        ).fetchall()
        techniciens = []

    return render_template(
        "home.html",
        incidents=incidents,
        user=session["user"].capitalize(),
        role=session["role"],
        techniciens=techniciens,
    )


@app.route("/api/home-content")
def home_content_api():
    if "user" not in session:
        return "", 403

    db = get_db()
    if session["role"] == "admin":
        incidents = db.execute("SELECT * FROM incidents WHERE archived=0").fetchall()
        techniciens = db.execute("SELECT * FROM techniciens").fetchall()
    else:
        incidents = db.execute(
            "SELECT * FROM incidents WHERE LOWER(collaborateur)=LOWER(?) AND archived=0",
            (session["user"],),
        ).fetchall()
        techniciens = []

    return render_template(
        "home_content.html",
        incidents=incidents,
        user=session["user"],
        role=session["role"],
        techniciens=techniciens,
    )


# ---------- GESTION DES TECHNICIENS (CRUD) ----------
@app.route("/techniciens")
def techniciens():
    if "user" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    db = get_db()
    techniciens = db.execute("SELECT * FROM techniciens").fetchall()
    return render_template("techniciens.html", techniciens=techniciens)


@app.route("/add_technicien", methods=["POST"])
def add_technicien():
    if "user" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    prenom = request.form["prenom"].strip()
    password = request.form["password"].strip()
    role = request.form.get("role", "technicien")

    db = get_db()
    db.execute(
        "INSERT INTO techniciens (prenom, role, password) VALUES (?, ?, ?)",
        (prenom, role, password),
    )
    db.commit()
    return redirect(url_for("techniciens"))


@app.route("/technicien/edit/<int:id>", methods=["POST"])
def edit_technicien(id):
    if "user" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    prenom = request.form["prenom"].strip()
    role = request.form.get("role", "technicien")
    new_pass = request.form.get("password", "").strip()

    db = get_db()
    if new_pass:
        db.execute(
            "UPDATE techniciens SET prenom=?, role=?, password=? WHERE id=?",
            (prenom, role, new_pass, id),
        )
    else:
        db.execute(
            "UPDATE techniciens SET prenom=?, role=? WHERE id=?",
            (prenom, role, id),
        )
    db.commit()
    return redirect(url_for("techniciens"))


@app.route("/technicien/incidents/<int:id>")
def technicien_incidents(id):
    if "user" not in session or session["role"] != "admin":
        return "", 403

    db = get_db()
    tech = db.execute("SELECT prenom FROM techniciens WHERE id=?", (id,)).fetchone()
    if not tech:
        return jsonify({"error": "Not found"}), 404

    incidents = db.execute(
        "SELECT * FROM incidents WHERE collaborateur=?", (tech["prenom"],)
    ).fetchall()
    autres_techs = db.execute(
        "SELECT id, prenom FROM techniciens WHERE id != ?", (id,)
    ).fetchall()

    return jsonify(
        {
            "incidents": [dict(i) for i in incidents],
            "autres_techs": [dict(t) for t in autres_techs],
            "tech_prenom": tech["prenom"],
        }
    )


@app.route("/technicien/transfer_delete/<int:id>", methods=["POST"])
def transfer_and_delete_technicien(id):
    if "user" not in session or session["role"] != "admin":
        return "", 403

    db = get_db()
    tech = db.execute("SELECT prenom FROM techniciens WHERE id=?", (id,)).fetchone()
    if not tech:
        return jsonify({"status": "error", "message": "Tech introuvable"}), 404

    # Ré-affecter chaque incident sélectionné, si applicable
    for key, value in request.form.items():
        if key.startswith("incident_"):
            incident_id = int(key.split("_")[1])
            nouveau_collab = value
            db.execute(
                "UPDATE incidents SET collaborateur=? WHERE id=?", (nouveau_collab, incident_id)
            )

    # Puis supprimer le technicien
    db.execute("DELETE FROM techniciens WHERE id=?", (id,))
    db.commit()
    return jsonify({"status": "ok"})


@app.route("/technicien/delete/<int:id>", methods=["POST"])
def delete_technicien(id):
    if "user" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    db = get_db()
    db.execute("DELETE FROM techniciens WHERE id=?", (id,))
    db.commit()
    return redirect(url_for("techniciens"))


# ----------- DRAG & DROP INCIDENTS (DASHBOARD ADMIN) -----------
@app.route("/incidents/assign", methods=["POST"])
def assign_incident():
    if "user" not in session or session["role"] != "admin":
        return "", 403

    incident_id = request.form.get("id")
    new_collab = request.form.get("collaborateur")
    if not incident_id or not new_collab:
        return jsonify({"status": "error", "message": "Paramètres manquants"}), 400

    db = get_db()
    db.execute(
        "UPDATE incidents SET collaborateur=? WHERE id=?", (new_collab, incident_id)
    )
    db.commit()
    socketio.emit("incident_update", {"action": "reassign"})
    return jsonify({"status": "ok"})


# ---------- EXPORTS AVANCÉS PAR TECHNICIEN ----------
@app.route("/export/pop_up")
def export_popup():
    if "user" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    db = get_db()
    techniciens = db.execute("SELECT id, prenom FROM techniciens").fetchall()
    return render_template("export_popup.html", techniciens=techniciens)


@app.route("/export/incidents/excel", methods=["POST"])
def export_incidents_excel():
    if "user" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    tech_ids = request.form.getlist("tech_ids")
    if not tech_ids:
        flash("Veuillez sélectionner au moins un technicien.", "warning")
        return redirect(url_for("export_popup"))

    db = get_db()
    placeholders = ",".join("?" for _ in tech_ids)
    query = f"SELECT prenom FROM techniciens WHERE id IN ({placeholders})"
    techs = [row["prenom"] for row in db.execute(query, tech_ids).fetchall()]

    if not techs:
        # Aucun technicien trouvé → on renvoie un DataFrame vide
        df = pd.DataFrame()
    else:
        params = ",".join("?" for _ in techs)
        sql = f"SELECT * FROM incidents WHERE collaborateur IN ({params}) AND archived=0"
        df = pd.read_sql_query(sql, db, params=techs)

    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Incidents")
    output.seek(0)

    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="incidents_filtrés.xlsx",
    )


@app.route("/export/incidents/pdf", methods=["POST"])
def export_incidents_pdf():
    if "user" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    tech_ids = request.form.getlist("tech_ids")
    if not tech_ids:
        flash("Veuillez sélectionner au moins un technicien.", "warning")
        return redirect(url_for("export_popup"))

    db = get_db()
    placeholders = ",".join("?" for _ in tech_ids)
    query = f"SELECT prenom FROM techniciens WHERE id IN ({placeholders})"
    techs = [row["prenom"] for row in db.execute(query, tech_ids).fetchall()]

    if not techs:
        incidents = []
    else:
        params = ",".join("?" for _ in techs)
        sql = f"SELECT * FROM incidents WHERE collaborateur IN ({params}) AND archived=0"
        incidents = db.execute(sql, techs).fetchall()

    html = render_template("export_pdf.html", incidents=incidents, techniciens=techs)

    try:
        pdf_data = pdfkit.from_string(html, False, configuration=pdf_config)
    except Exception as e:
        app.logger.error(f"Erreur wkhtmltopdf: {e}")
        flash(
            "La génération du PDF a échoué : vérifiez l’installation de wkhtmltopdf.",
            "danger",
        )
        return redirect(url_for("stats"))

    return send_file(
        BytesIO(pdf_data),
        mimetype="application/pdf",
        as_attachment=True,
        download_name="incidents_filtrés.pdf",
    )


# ---------- AUTH (users + techniciens) ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form["username"].strip()
        p = request.form["password"].strip()
        db = get_db()

        # 1) Essayer dans users
        user = db.execute(
            "SELECT * FROM users WHERE username=? AND password=?", (u, p)
        ).fetchone()
        if user:
            session["user"] = u
            session["role"] = user["role"]
            session.permanent = True
            return redirect(url_for("home"))

        # 2) Sinon, essayer dans techniciens
        tech = db.execute(
            "SELECT * FROM techniciens WHERE LOWER(prenom)=LOWER(?) AND password=?",
            (u, p),
        ).fetchone()
        if tech:
            session["user"] = tech["prenom"]
            session["role"] = tech["role"] or "technicien"
            session.permanent = True
            return redirect(url_for("home"))

        flash("Mauvais identifiants", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- AJOUT INCIDENT ----------
@app.route("/add", methods=["GET", "POST"])
def add_incident():
    if "user" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    db = get_db()
    techniciens = db.execute("SELECT * FROM techniciens").fetchall()

    if request.method == "POST":
        numero = request.form["numero"]
        site = request.form["site"]
        sujet = request.form["sujet"]
        urgence = request.form["urgence"]
        collab = request.form["collaborateur"]
        date_aff = request.form["date_affectation"]

        sql = """
          INSERT INTO incidents (
            numero, site, sujet, urgence,
            collaborateur, etat, notes,
            valide, date_affectation, archived
          ) VALUES (?, ?, ?, ?, ?, 'Affecté', '', 0, ?, 0)
        """
        db.execute(sql, (numero, site, sujet, urgence, collab, date_aff))
        db.commit()
        socketio.emit("incident_update", {"action": "add"})
        return redirect(url_for("home"))

    current = datetime.now().strftime("%Y-%m-%d")
    return render_template(
        "add_incident.html", current_date=current, techniciens=techniciens
    )


# ---------- NOTES INCIDENTS ----------
@app.route("/edit_note/<int:id>", methods=["GET", "POST"])
def edit_note(id):
    if "user" not in session:
        return redirect(url_for("login"))

    db = get_db()
    inc = db.execute("SELECT * FROM incidents WHERE id=?", (id,)).fetchone()
    if inc["collaborateur"].lower() != session["user"].lower() and session["role"] != "admin":
        return redirect(url_for("home"))

    if request.method == "POST":
        note = request.form["note"] or ""
        if inc["notes"] != note:
            db.execute("UPDATE incidents SET notes=? WHERE id=?", (note, id))
            hist_sql = """
              INSERT INTO historique (
                incident_id, champ, ancienne_valeur,
                nouvelle_valeur, modifie_par, date_modification
              ) VALUES (?, ?, ?, ?, ?, ?)
            """
            db.execute(
                hist_sql,
                (
                    id,
                    "notes",
                    inc["notes"],
                    note,
                    session["user"],
                    datetime.now().strftime("%d-%m-%Y %H:%M"),
                ),
            )
            db.commit()
            socketio.emit("incident_update", {"action": "note"})

        return redirect(url_for("home"))

    return render_template("edit_note.html", id=id, current_note=inc["notes"])


# ---------- UPDATE ETAT ----------
@app.route("/update_etat/<int:id>", methods=["POST"])
def update_etat(id):
    if "user" not in session:
        return redirect(url_for("login"))

    db = get_db()
    inc = db.execute("SELECT * FROM incidents WHERE id=?", (id,)).fetchone()
    new = request.form["etat"]

    if inc["etat"] != new:
        db.execute("UPDATE incidents SET etat=? WHERE id=?", (new, id))
        hist_sql = """
          INSERT INTO historique (
            incident_id, champ, ancienne_valeur,
            nouvelle_valeur, modifie_par, date_modification
          ) VALUES (?, ?, ?, ?, ?, ?)
        """
        db.execute(
            hist_sql,
            (
                id,
                "etat",
                inc["etat"],
                new,
                session["user"],
                datetime.now().strftime("%d-%m-%Y %H:%M"),
            ),
        )
        db.commit()
        socketio.emit("incident_update", {"action": "etat"})

    return redirect(url_for("home"))


# ---------- VALIDATION ----------
@app.route("/valider/<int:id>", methods=["POST"])
def valider(id):
    if "user" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    val = 1 if request.form.get("valide") == "on" else 0
    db = get_db()
    db.execute("UPDATE incidents SET valide=? WHERE id=?", (val, id))
    db.commit()
    socketio.emit("incident_update", {"action": "valide"})
    return redirect(url_for("home"))


# ---------- SUPPRESSION INCIDENT ----------
@app.route("/delete/<int:id>", methods=["POST"])
def delete(id):
    if "user" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    db = get_db()
    db.execute("DELETE FROM incidents WHERE id=?", (id,))
    db.commit()
    socketio.emit("incident_update", {"action": "delete"})
    return redirect(url_for("home"))


# ---------- ARCHIVE INCIDENT ----------
@app.route("/archive/<int:id>", methods=["POST"])
def archive(id):
    if "user" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    db = get_db()
    db.execute("UPDATE incidents SET archived=1 WHERE id=?", (id,))
    db.commit()
    socketio.emit("incident_update", {"action": "archive"})
    return redirect(url_for("home"))


# ---------- HISTORIQUE ----------
@app.route("/historique/<int:id>")
def historique(id):
    if "user" not in session:
        return redirect(url_for("login"))

    logs = get_db().execute(
        "SELECT * FROM historique WHERE incident_id=? ORDER BY date_modification DESC", (id,)
    ).fetchall()
    return render_template("historique.html", logs=logs, id=id)


# ---------- STATS : page principale (injection techniciens + statuts) ----------
@app.route("/stats")
def stats():
    if "user" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    db = get_db()
    tech_rows = db.execute("SELECT prenom FROM techniciens").fetchall()
    techniciens_list = [r["prenom"] for r in tech_rows]

    statuts_rows = db.execute("SELECT DISTINCT etat FROM incidents WHERE archived=0").fetchall()
    statuts = [r["etat"] for r in statuts_rows]

    return render_template(
        "stats.html",
        techniciens_list=techniciens_list,
        statuts=statuts,
        role=session["role"],
    )


# ---------- STATS DATA : renvoie JSON en fonction des filtres ----------
@app.route("/stats/data")
def stats_data():
    if "user" not in session or session["role"] != "admin":
        return jsonify({"error": "forbidden"}), 403

    db = get_db()
    start = request.args.get("start")
    end = request.args.get("end")
    techs = request.args.get("tech", "")    # chaîne CSV "Alice,Bob"
    stats = request.args.get("status", "")  # chaîne CSV "Traité,Restant"

    # Conditions de base : incident non archivé + collaborateur réel
    conditions = [
        "archived=0",
        "collaborateur IS NOT NULL",
        "TRIM(collaborateur) != ''",
        "collaborateur != 'Non affecté'"
    ]
    params = []

    if start:
        conditions.append("date_affectation >= ?")
        params.append(start)
    if end:
        conditions.append("date_affectation <= ?")
        params.append(end)

    if techs:
        lst = techs.split(",")
        placeholders = ",".join("?" for _ in lst)
        conditions.append(f"LOWER(collaborateur) IN ({placeholders})")
        params.extend([t.strip().lower() for t in lst])

    if stats:
        lst_s = stats.split(",")
        placeholders_s = ",".join("?" for _ in lst_s)
        conditions.append(f"etat IN ({placeholders_s})")
        params.extend([s.strip() for s in lst_s])

    where_clause = " AND ".join(conditions)

    # Récupérer la liste de tous les techniciens (pour légende du BarChart)
    tech_rows = db.execute("SELECT prenom FROM techniciens").fetchall()
    techniciens_list = [r["prenom"] for r in tech_rows]

    # Construire dataParTech : nombre d’incidents par technicien × statut
    sql_tech = f"""
        SELECT LOWER(collaborateur) AS prenom, etat, COUNT(*) AS total
        FROM incidents
        WHERE {where_clause}
        GROUP BY LOWER(collaborateur), etat
    """
    rows_tech = db.execute(sql_tech, params).fetchall()
    dataParTech = {t: {} for t in techniciens_list}
    for r in rows_tech:
        p = r["prenom"]
        vrai_prenom = next((x for x in techniciens_list if x.lower() == p), p.capitalize())
        dataParTech[vrai_prenom][r["etat"]] = r["total"]

    # Liste des statuts distincts (après filtrage)
    statuts_rows = db.execute(
        f"SELECT DISTINCT etat FROM incidents WHERE {where_clause}", params
    ).fetchall()
    statuts_list = [r["etat"] for r in statuts_rows]

    # Évolution hebdo "Restants" & "Transférés/Réservés" sur les 7 derniers jours
    date_rows = db.execute(
        f"""
        SELECT DISTINCT date_affectation AS d
        FROM incidents
        WHERE {where_clause}
        ORDER BY date_affectation ASC
        LIMIT 7
        """, params
    ).fetchall()
    dates7 = [r["d"] for r in date_rows]

    # Si moins de 7 dates, on force les 7 derniers jours jusqu’à aujourd’hui
    if len(dates7) < 7:
        max_date_row = db.execute(
            f"SELECT MAX(date_affectation) AS dmax FROM incidents WHERE {where_clause}", params
        ).fetchone()
        if max_date_row and max_date_row["dmax"]:
            dmax = datetime.strptime(max_date_row["dmax"], "%Y-%m-%d").date()
            dates7 = [(dmax - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
        else:
            today = datetime.now().date()
            dates7 = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]

    values_rest = []
    values_transf = []
    for d in dates7:
        cond_date = f"{where_clause} AND date_affectation = ?"
        p_date = params + [d]

        row_r = db.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM incidents
            WHERE {cond_date}
              AND etat IN ('Affecté','En cours de préparation','Suspendu')
            """, p_date
        ).fetchone()
        values_rest.append(row_r["total"] if row_r else 0)

        row_t = db.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM incidents
            WHERE {cond_date}
              AND etat IN ('Transféré','En réservation')
            """, p_date
        ).fetchone()
        values_transf.append(row_t["total"] if row_t else 0)

    # Tables quotidiennes "Traités" et "Restants"
    sql_traites = f"""
        SELECT date_affectation, site, sujet, COUNT(*) AS total
        FROM incidents
        WHERE {where_clause} AND etat='Traité'
        GROUP BY date_affectation, site, sujet
        ORDER BY date_affectation DESC
    """
    rows_traites = db.execute(sql_traites, params).fetchall()
    traiteurs = [dict(r) for r in rows_traites]

    sql_restants_daily = f"""
        SELECT date_affectation, site, sujet,
            SUM(CASE WHEN etat='Affecté' THEN 1 ELSE 0 END)            AS affecte,
            SUM(CASE WHEN etat='En cours de préparation' THEN 1 ELSE 0 END) AS en_cours,
            SUM(CASE WHEN etat='Suspendu' THEN 1 ELSE 0 END)            AS suspendu
        FROM incidents
        WHERE {where_clause}
        GROUP BY date_affectation, site, sujet
        ORDER BY date_affectation DESC
    """
    rows_restants_daily = db.execute(sql_restants_daily, params).fetchall()
    restants_daily = [dict(r) for r in rows_restants_daily]

    # Calcul des pourcentages J-1 vs J-2
    today = datetime.now().date()
    str_y = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    str_db = (today - timedelta(days=2)).strftime("%Y-%m-%d")

    row_y_tr = db.execute(
        f"SELECT COUNT(*) AS total FROM incidents WHERE {where_clause} AND date_affectation = ? AND etat='Traité'",
        params + [str_y]
    ).fetchone()
    row_db_tr = db.execute(
        f"SELECT COUNT(*) AS total FROM incidents WHERE {where_clause} AND date_affectation = ? AND etat='Traité'",
        params + [str_db]
    ).fetchone()
    tot_y_tr = row_y_tr["total"] if row_y_tr else 0
    tot_db_tr = row_db_tr["total"] if row_db_tr else 0
    percent_traites = None
    if tot_db_tr > 0:
        percent_traites = round((tot_y_tr - tot_db_tr) / tot_db_tr * 100, 1)

    row_y_res = db.execute(
        f"SELECT COUNT(*) AS total FROM incidents WHERE {where_clause} AND date_affectation = ? AND etat IN ('Affecté','En cours de préparation','Suspendu')",
        params + [str_y]
    ).fetchone()
    row_db_res = db.execute(
        f"SELECT COUNT(*) AS total FROM incidents WHERE {where_clause} AND date_affectation = ? AND etat IN ('Affecté','En cours de préparation','Suspendu')",
        params + [str_db]
    ).fetchone()
    tot_y_res = row_y_res["total"] if row_y_res else 0
    tot_db_res = row_db_res["total"] if row_db_res else 0
    percent_restants = None
    if tot_db_res > 0:
        percent_restants = round((tot_y_res - tot_db_res) / tot_db_res * 100, 1)

    row_y_trf = db.execute(
        f"SELECT COUNT(*) AS total FROM incidents WHERE {where_clause} AND date_affectation = ? AND etat IN ('Transféré','En réservation')",
        params + [str_y]
    ).fetchone()
    row_db_trf = db.execute(
        f"SELECT COUNT(*) AS total FROM incidents WHERE {where_clause} AND date_affectation = ? AND etat IN ('Transféré','En réservation')",
        params + [str_db]
    ).fetchone()
    tot_y_trf = row_y_trf["total"] if row_y_trf else 0
    tot_db_trf = row_db_trf["total"] if row_db_trf else 0
    percent_transf = None
    if tot_db_trf > 0:
        percent_transf = round((tot_y_trf - tot_db_trf) / tot_db_trf * 100, 1)

    return jsonify(
        {
            "techniciens": techniciens_list,
            "dataParTech": dataParTech,
            "statuts": statuts_list,
            "dates_restants": dates7,
            "values_restants": values_rest,
            "dates_transfres": dates7,
            "values_transfres": values_transf,
            "traites": traiteurs,
            "restants_daily": restants_daily,
            "percent_traites": percent_traites,
            "percent_restants": percent_restants,
            "percent_transf": percent_transf,
        }
    )


# ---------- STATISTIQUES BONUS PAR TECHNICIEN ET PAR STATUT ----------
@app.route("/stats/bonus")
def stats_bonus():
    if "user" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    db = get_db()
    techniciens = [t["prenom"] for t in db.execute("SELECT prenom FROM techniciens").fetchall()]

    data_par_tech = {}
    for t in techniciens:
        rows = db.execute(
            "SELECT etat, COUNT(*) AS total "
            "FROM incidents "
            "WHERE LOWER(collaborateur)=LOWER(?) AND archived=0 "
            "GROUP BY etat",
            (t,),
        ).fetchall()
        sous_dict = {
            "Affecté": 0,
            "En cours de préparation": 0,
            "Traité": 0,
            "Suspendu": 0,
            "En réservation": 0,
            "Transféré": 0,
        }
        for r in rows:
            if r["etat"] in sous_dict:
                sous_dict[r["etat"]] = r["total"]
        data_par_tech[t] = sous_dict

    statuts = ["Affecté", "En cours de préparation", "Traité", "Suspendu", "En réservation", "Transféré"]

    restants_par_jour = db.execute(
        """
        SELECT date_affectation,
               SUM(CASE WHEN etat IN ('Affecté','En cours de préparation','Suspendu') THEN 1 ELSE 0 END) AS restants
        FROM incidents
        WHERE archived=0
        GROUP BY date_affectation
        ORDER BY date_affectation ASC
        """
    ).fetchall()

    transfres_par_jour = db.execute(
        """
        SELECT date_affectation,
               SUM(CASE WHEN etat IN ('Transféré','En réservation') THEN 1 ELSE 0 END) AS transfres
        FROM incidents
        WHERE archived=0
        GROUP BY date_affectation
        ORDER BY date_affectation ASC
        """
    ).fetchall()

    return render_template(
        "stats_bonus.html",
        techniciens=techniciens,
        data_par_tech=data_par_tech,
        statuts=statuts,
        restants_par_jour=restants_par_jour,
        transfres_par_jour=transfres_par_jour,
    )


# ---------- EXPORT EXCEL DU TABLEAU STATS EXISTANT ----------
@app.route("/stats/export/excel")
def export_stats_excel():
    if "user" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    db = get_db()
    df1 = pd.read_sql_query(
        "SELECT site, sujet, COUNT(*) AS total FROM incidents WHERE archived=0 GROUP BY site, sujet", db
    )
    df2 = pd.read_sql_query(
        """
        SELECT date_affectation, site, sujet, COUNT(*) AS total
        FROM incidents
        WHERE etat='Traité' AND archived=0
        GROUP BY date_affectation, site, sujet
        """, db
    )
    df3 = pd.read_sql_query(
        """
        SELECT date_affectation, site, sujet,
               SUM(CASE WHEN etat='Affecté' THEN 1 ELSE 0 END) AS affecte,
               SUM(CASE WHEN etat='En cours de préparation' THEN 1 ELSE 0 END) AS en_cours,
               SUM(CASE WHEN etat='Suspendu' THEN 1 ELSE 0 END) AS suspendu
        FROM incidents
        WHERE archived=0
        GROUP BY date_affectation, site, sujet
        """, db
    )

    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df1.to_excel(writer, sheet_name="Sujets par site", index=False)
        df2.to_excel(writer, sheet_name="Traités par jour", index=False)
        df3.to_excel(writer, sheet_name="Restants par jour", index=False)
    output.seek(0)

    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="stats.xlsx",
    )


# ---------- EXPORT PDF DU TABLEAU STATS EXISTANT ----------
@app.route("/stats/export/pdf")
def export_stats_pdf():
    if "user" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    db = get_db()
    sujets_sites = db.execute(
        "SELECT site,sujet,COUNT(*) as total FROM incidents WHERE archived=0 GROUP BY site,sujet"
    ).fetchall()
    traites = db.execute(
        """
        SELECT date_affectation, site, sujet, COUNT(*) as total
        FROM incidents
        WHERE etat='Traité' AND archived=0
        GROUP BY date_affectation, site, sujet
        """
    ).fetchall()
    restants = db.execute(
        """
        SELECT date_affectation, site, sujet,
               SUM(CASE WHEN etat='Affecté' THEN 1 ELSE 0 END) as affecte,
               SUM(CASE WHEN etat='En cours de préparation' THEN 1 ELSE 0 END) as en_cours,
               SUM(CASE WHEN etat='Suspendu' THEN 1 ELSE 0 END) as suspendu
        FROM incidents
        WHERE archived=0
        GROUP BY date_affectation, site, sujet
        """
    ).fetchall()

    # Render the HTML template for the PDF
    html = render_template(
        "export_pdf.html",
        sujets_sites=sujets_sites,
        traites=traites,
        restants=restants,
    )

    # Options pour wkhtmltopdf sous Windows
    options = {
        "enable-local-file-access": None,
        "page-size": "A4",
        "margin-top": "0.75in",
        "margin-right": "0.75in",
        "margin-bottom": "0.75in",
        "margin-left": "0.75in",
    }

    try:
        pdf_data = pdfkit.from_string(
            html,
            False,
            configuration=pdf_config,
            options=options
        )
    except Exception as e:
        app.logger.error(f"Erreur wkhtmltopdf lors de l'export PDF statistiques : {e}")
        flash("La génération du PDF des statistiques a échoué (vérifiez la configuration de wkhtmltopdf).", "danger")
        return redirect(url_for("stats"))

    return send_file(
        BytesIO(pdf_data),
        mimetype="application/pdf",
        as_attachment=True,
        download_name="stats.pdf",
    )


# ---------- DETAILS ----------
@app.route("/details")
def details():
    if "user" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    date = request.args.get("date")
    site = request.args.get("site")
    sujet = request.args.get("sujet")
    ttype = request.args.get("type")

    db = get_db()
    # On commence par filtrer date + site + sujet
    query = "SELECT * FROM incidents WHERE date_affectation=? AND site=? AND sujet=? AND archived=0"
    params = [date, site, sujet]

    # On ajoute ensuite le filtre sur l'état
    if ttype == "traite":
        query += " AND etat='Traité'"
    else:
        query += " AND etat IN ('Affecté','En cours de préparation','Suspendu')"

    incs = db.execute(query, params).fetchall()
    return render_template("details.html",
                           incidents=incs,
                           date=date,
                           site=site,
                           sujet=sujet,
                           type=ttype)

# ---------- ARCHIVES ----------
@app.route("/archives")
def archives():
    if "user" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    archived = get_db().execute("SELECT * FROM incidents WHERE archived=1").fetchall()
    return render_template("archives.html", incidents=archived)


# ---------- SOCKET.IO ----------
@socketio.on("join")
def on_join(data):
    join_room(data["room"])


@socketio.on("refresh_all")
def refresh_all():
    emit("incident_update", {"action": "refresh"}, broadcast=True)


# ---------- INIT DB ----------
if __name__ == "__main__":
    with get_db() as db:
        try:
            db.execute("ALTER TABLE techniciens ADD COLUMN role TEXT DEFAULT 'technicien'")
            db.commit()
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("ALTER TABLE techniciens ADD COLUMN password TEXT")
            db.commit()
        except sqlite3.OperationalError:
            pass

    if not os.path.exists(DB):
        db = get_db()
        db.execute(
            """
            CREATE TABLE techniciens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prenom TEXT UNIQUE NOT NULL,
                role TEXT DEFAULT 'technicien',
                password TEXT
            );
            """
        )
        db.execute(
            """
            CREATE TABLE incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                numero TEXT,
                site TEXT,
                sujet TEXT,
                urgence TEXT,
                collaborateur TEXT,
                etat TEXT,
                notes TEXT,
                valide INTEGER,
                date_affectation TEXT,
                archived INTEGER DEFAULT 0
            );
            """
        )
        db.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                password TEXT,
                role TEXT
            );
            """
        )
        db.execute(
            """
            CREATE TABLE historique (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id INTEGER,
                champ TEXT,
                ancienne_valeur TEXT,
                nouvelle_valeur TEXT,
                modifie_par TEXT,
                date_modification TEXT
            );
            """
        )
        db.execute(
            """
            INSERT INTO techniciens (prenom, role, password) VALUES
            ('Hugo','technicien',''),
            ('Steeve','technicien',''),
            ('Patrice','technicien',''),
            ('Aurélien','technicien','');
            """
        )
        db.execute(
            """
            INSERT INTO users (username,password,role) VALUES
            ('melvin','admin','admin'),
            ('hugo','hugo','user'),
            ('steeve','steeve','user'),
            ('patrice','patrice','user'),
            ('aurelien','aurelien','user');
            """
        )
        db.commit()

    socketio.run(app, host="0.0.0.0", port=3000, debug=False)