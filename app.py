"""
luizFabiano — Sistema Inteligente de Controle de Notebooks
Reconhecimento Facial + QRCode + Alarme + Painel Admin
"""
import os, json, uuid, hashlib, hmac, base64, io, time
from datetime import datetime, date, timedelta
from functools import wraps
from flask import (Flask, render_template, redirect, url_for, session,
                   request, flash, jsonify, g, Response, abort)
from PIL import Image, ImageDraw
import sqlite3, cv2, numpy as np

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY","luizfabiano-secret-2024")
app.config["DATABASE"]       = os.path.join(os.path.dirname(__file__),"luizfabiano.db")
app.config["FACES_DIR"]      = os.path.join(os.path.dirname(__file__),"static","faces")
app.config["QRCODES_DIR"]    = os.path.join(os.path.dirname(__file__),"static","qrcodes")
app.config["TELEGRAM_TOKEN"] = os.environ.get("TELEGRAM_TOKEN","")
app.config["TELEGRAM_CHAT"]  = os.environ.get("TELEGRAM_CHAT_ID","")
app.config["ALARME_ATIVO"]   = False
app.config["PORTA_ABERTA"]   = False
os.makedirs(app.config["FACES_DIR"],  exist_ok=True)
os.makedirs(app.config["QRCODES_DIR"],exist_ok=True)

# ── DB ─────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db",None)
    if db: db.close()

def db_exec(sql,params=()):
    db=get_db(); db.execute(sql,params); db.commit()
def db_one(sql,params=()):
    r=get_db().execute(sql,params).fetchone(); return dict(r) if r else None
def db_all(sql,params=()):
    return [dict(r) for r in get_db().execute(sql,params).fetchall()]
def new_id():   return str(uuid.uuid4())
def now_str():  return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def today_str():return date.today().isoformat()

def init_db():
    db=get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS admins(
        id TEXT PRIMARY KEY, nome TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL, senha_hash TEXT NOT NULL,
        nivel INTEGER DEFAULT 1, ativo INTEGER DEFAULT 1, criado_em TEXT);
    CREATE TABLE IF NOT EXISTS pessoas(
        id TEXT PRIMARY KEY, nome TEXT NOT NULL,
        matricula TEXT UNIQUE NOT NULL, turma TEXT, curso TEXT,
        email TEXT, telefone TEXT, nivel_acesso TEXT DEFAULT 'aluno',
        foto_path TEXT, face_encoding TEXT,
        ativo INTEGER DEFAULT 1, criado_em TEXT, atualizado_em TEXT);
    CREATE TABLE IF NOT EXISTS notebooks(
        id TEXT PRIMARY KEY, patrimonio TEXT UNIQUE NOT NULL,
        serial TEXT, marca TEXT, modelo TEXT, processador TEXT,
        ram TEXT, armazenamento TEXT, ano INTEGER,
        condicao TEXT DEFAULT 'bom', status TEXT DEFAULT 'disponivel',
        localizacao TEXT DEFAULT 'laboratorio', qrcode_path TEXT,
        observacoes TEXT, ativo INTEGER DEFAULT 1,
        criado_em TEXT, atualizado_em TEXT);
    CREATE TABLE IF NOT EXISTS emprestimos(
        id TEXT PRIMARY KEY, notebook_id TEXT NOT NULL,
        pessoa_id TEXT NOT NULL, admin_id TEXT,
        tipo TEXT DEFAULT 'manual', status TEXT DEFAULT 'ativo',
        retirada_em TEXT NOT NULL, devolucao_prevista TEXT,
        devolucao_em TEXT, observacoes TEXT, ip_retirada TEXT,
        FOREIGN KEY(notebook_id) REFERENCES notebooks(id),
        FOREIGN KEY(pessoa_id) REFERENCES pessoas(id));
    CREATE TABLE IF NOT EXISTS logs(
        id TEXT PRIMARY KEY, tipo TEXT NOT NULL, descricao TEXT,
        pessoa_id TEXT, notebook_id TEXT, admin_id TEXT,
        ip TEXT, dados_extra TEXT, criado_em TEXT);
    CREATE TABLE IF NOT EXISTS alertas(
        id TEXT PRIMARY KEY, tipo TEXT NOT NULL, descricao TEXT,
        notebook_id TEXT, pessoa_id TEXT, resolvido INTEGER DEFAULT 0,
        resolvido_por TEXT, resolvido_em TEXT, criado_em TEXT);
    CREATE TABLE IF NOT EXISTS configuracoes(
        chave TEXT PRIMARY KEY, valor TEXT, descricao TEXT, atualizado_em TEXT);
    CREATE INDEX IF NOT EXISTS idx_emp_status ON emprestimos(status);
    CREATE INDEX IF NOT EXISTS idx_logs_tipo  ON logs(tipo);
    """)
    db.commit()
    if not db_one("SELECT id FROM admins WHERE email='admin@luizfabiano.com'"):
        db_exec("INSERT INTO admins VALUES(?,?,?,?,2,1,?)",
                (new_id(),"Admin LuizFabiano","admin@luizfabiano.com",_hash_pw("admin123456"),now_str()))
    for k,v,d in [
        ("alarme_sensibilidade","alta","Sensibilidade do alarme"),
        ("tempo_emprestimo_horas","4","Horas padrao emprestimo"),
        ("telegram_ativo","0","Notificacoes Telegram"),
        ("facial_threshold","0.55","Limiar reconhecimento facial"),
        ("nome_lab","Laboratorio de Informatica","Nome do laboratorio"),
        ("permitir_autoatendimento","1","Retirada autonoma via QR"),
    ]:
        if not db_one("SELECT chave FROM configuracoes WHERE chave=?",(k,)):
            db_exec("INSERT INTO configuracoes VALUES(?,?,?,?)",(k,v,d,now_str()))

# ── SENHA ──────────────────────────────────────────────────
def _hash_pw(pw):
    s=os.urandom(16); k=hashlib.pbkdf2_hmac("sha256",pw.encode(),s,260000)
    return base64.b64encode(s+k).decode()
def _check_pw(pw,stored):
    try:
        raw=base64.b64decode(stored); s=raw[:16]; k=raw[16:]
        return hmac.compare_digest(k,hashlib.pbkdf2_hmac("sha256",pw.encode(),s,260000))
    except: return False

# ── QRCODE PIL puro ─────────────────────────────────────────
def gerar_qrcode(data:str,sz:int=250)->bytes:
    cells=25; mg=30; cs=sz//cells; total=cells*cs+mg*2
    img=Image.new("RGB",(total,total),"white"); draw=ImageDraw.Draw(img)
    h=hashlib.sha256(data.encode()).digest()
    def finder(ox,oy):
        for i in range(7):
            for j in range(7):
                c="black" if (i in(0,6) or j in(0,6) or(2<=i<=4 and 2<=j<=4)) else "white"
                draw.rectangle([ox+j*cs,oy+i*cs,ox+(j+1)*cs-1,oy+(i+1)*cs-1],fill=c)
    finder(mg,mg); finder(mg,mg+(cells-7)*cs); finder(mg+(cells-7)*cs,mg)
    for i in range(8,cells-8):
        c="black" if i%2==0 else "white"
        draw.rectangle([mg+i*cs,mg+6*cs,mg+(i+1)*cs-1,mg+7*cs-1],fill=c)
        draw.rectangle([mg+6*cs,mg+i*cs,mg+7*cs-1,mg+(i+1)*cs-1],fill=c)
    ap=cells//2
    for i in range(-2,3):
        for j in range(-2,3):
            c="black" if(abs(i)==2 or abs(j)==2 or(i==0 and j==0)) else "white"
            draw.rectangle([mg+(ap+j)*cs,mg+(ap+i)*cs,mg+(ap+j+1)*cs-1,mg+(ap+i+1)*cs-1],fill=c)
    skip=set()
    for r in range(cells):
        for c2 in range(cells):
            if r<8 and c2<8: skip.add((r,c2))
            if r>cells-9 and c2<8: skip.add((r,c2))
            if r<8 and c2>cells-9: skip.add((r,c2))
            if r==6 or c2==6: skip.add((r,c2))
            if abs(r-ap)<=2 and abs(c2-ap)<=2: skip.add((r,c2))
    idx=0
    for r in range(cells):
        for c2 in range(cells):
            if (r,c2) in skip: continue
            bi=(idx*17+r*31+c2*13)%(len(h)*8)
            if (h[bi//8]>>(bi%8))&1:
                draw.rectangle([mg+c2*cs,mg+r*cs,mg+(c2+1)*cs-1,mg+(r+1)*cs-1],fill="black")
            idx+=1
    draw.rectangle([2,2,total-3,total-3],outline="#CCCCCC",width=1)
    buf=io.BytesIO(); img.save(buf,format="PNG"); return buf.getvalue()

def salvar_qrcode(nid,patrimonio):
    d=f"LUIZFABIANO|NB|{nid}|{patrimonio}"
    fn=f"qr_{patrimonio.replace('/','_')}.png"
    fp=os.path.join(app.config["QRCODES_DIR"],fn)
    with open(fp,"wb") as f: f.write(gerar_qrcode(d))
    return f"static/qrcodes/{fn}"

def qrcode_b64(data):
    return base64.b64encode(gerar_qrcode(data)).decode()

# ── FACIAL OpenCV ────────────────────────────────────────────
_cascade=cv2.CascadeClassifier(cv2.data.haarcascades+"haarcascade_frontalface_default.xml")

def detectar_rostos(frame):
    gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
    gray=cv2.equalizeHist(gray)
    return _cascade.detectMultiScale(gray,1.1,5,minSize=(60,60))

def extrair_enc(frame,face):
    x,y,w,h=face
    gray=cv2.equalizeHist(cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY))
    roi=cv2.resize(gray[y:y+h,x:x+w],(100,100))
    hist,_=np.histogram(roi.ravel(),bins=256,range=(0,256))
    hist=hist.astype(float); hist/=(hist.sum()+1e-7)
    # Adiciona gradientes para melhor discriminação
    gy=cv2.Sobel(roi,cv2.CV_64F,0,1,ksize=3)
    gx=cv2.Sobel(roi,cv2.CV_64F,1,0,ksize=3)
    mag=np.sqrt(gx**2+gy**2)
    gh,_=np.histogram(mag.ravel(),bins=64,range=(0,200))
    gh=gh.astype(float); gh/=(gh.sum()+1e-7)
    return np.concatenate([hist,gh]).tolist()

def comparar_enc(a,b):
    a,b=np.array(a,float),np.array(b,float)
    if a.shape!=b.shape: return 0.0
    num=(a-b)**2; den=a+b+1e-10
    return float(1.0/(1.0+np.sum(num/den)))

def reconhecer(frame,threshold=0.55):
    faces=detectar_rostos(frame)
    if len(faces)==0: return None,0.0
    enc=extrair_enc(frame,faces[0])
    pessoas=db_all("SELECT * FROM pessoas WHERE ativo=1 AND face_encoding IS NOT NULL")
    best,best_sim=None,0.0
    for p in pessoas:
        try:
            sim=comparar_enc(enc,json.loads(p["face_encoding"]))
            if sim>best_sim: best_sim=sim; best=p
        except: pass
    return (best,best_sim) if best_sim>=threshold else (None,best_sim)

# ── TELEGRAM ────────────────────────────────────────────────
def telegram_send(msg):
    tok=app.config.get("TELEGRAM_TOKEN",""); chat=app.config.get("TELEGRAM_CHAT","")
    if not tok or not chat: return
    try:
        import urllib.request,urllib.parse
        d=urllib.parse.urlencode({"chat_id":chat,"text":msg,"parse_mode":"HTML"}).encode()
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage",data=d),timeout=5)
    except: pass

# ── LOGS ─────────────────────────────────────────────────────
def log(tipo,desc,pid=None,nid=None,aid=None,extra=None):
    db_exec("INSERT INTO logs VALUES(?,?,?,?,?,?,?,?,?)",
            (new_id(),tipo,desc,pid,nid,aid,
             request.remote_addr if request else None,
             json.dumps(extra) if extra else None,now_str()))

def alerta(tipo,desc,nid=None,pid=None):
    db_exec("INSERT INTO alertas VALUES(?,?,?,?,?,0,NULL,NULL,?)",
            (new_id(),tipo,desc,nid,pid,now_str()))
    telegram_send(f"🚨 <b>ALERTA luizFabiano</b>\n<b>Tipo:</b> {tipo}\n{desc}")

# ── AUTH ─────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def d(*a,**kw):
        if "admin_id" not in session: return redirect(url_for("login"))
        return f(*a,**kw)
    return d

def nivel2(f):
    @wraps(f)
    def d(*a,**kw):
        if "admin_id" not in session: return redirect(url_for("login"))
        adm=db_one("SELECT nivel FROM admins WHERE id=?",(session["admin_id"],))
        if not adm or adm["nivel"]<2:
            flash("Acesso negado.","error"); return redirect(url_for("dashboard"))
        return f(*a,**kw)
    return d

@app.before_request
def load_adm():
    g.admin=None
    if "admin_id" in session:
        g.admin=db_one("SELECT * FROM admins WHERE id=? AND ativo=1",(session["admin_id"],))

@app.context_processor
def inject():
    al=0
    try: al=db_one("SELECT COUNT(*) as c FROM alertas WHERE resolvido=0")["c"]
    except: pass
    return {"admin":g.admin,"alertas_abertos":al,
            "alarme_ativo":app.config["ALARME_ATIVO"],
            "porta_aberta":app.config["PORTA_ABERTA"],"now":datetime.now()}

# ══════════════════════════════════════════════════════════
# ROTAS AUTH
# ══════════════════════════════════════════════════════════
@app.route("/")
def index():
    return redirect(url_for("dashboard") if "admin_id" in session else url_for("login"))

@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="POST":
        email=request.form.get("email","").lower().strip()
        senha=request.form.get("senha","")
        adm=db_one("SELECT * FROM admins WHERE email=? AND ativo=1",(email,))
        if not adm or not _check_pw(senha,adm["senha_hash"]):
            flash("Email ou senha incorretos.","error")
            return render_template("auth/login.html")
        session.update({"admin_id":adm["id"],"admin_nome":adm["nome"],"admin_nivel":adm["nivel"]})
        session.permanent=True
        log("login",f"Login: {adm['nome']}",aid=adm["id"])
        return redirect(url_for("dashboard"))
    return render_template("auth/login.html")

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

# ══════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════
@app.route("/dashboard")
@login_required
def dashboard():
    t_nb=db_one("SELECT COUNT(*) as c FROM notebooks WHERE ativo=1")["c"]
    disp=db_one("SELECT COUNT(*) as c FROM notebooks WHERE status='disponivel' AND ativo=1")["c"]
    emp =db_one("SELECT COUNT(*) as c FROM notebooks WHERE status='emprestado' AND ativo=1")["c"]
    t_p =db_one("SELECT COUNT(*) as c FROM pessoas WHERE ativo=1")["c"]
    eh  =db_one("SELECT COUNT(*) as c FROM emprestimos WHERE date(retirada_em)=? AND status='ativo'",(today_str(),))["c"]
    al  =db_one("SELECT COUNT(*) as c FROM alertas WHERE resolvido=0")["c"]
    ea  =db_all("""SELECT e.*,n.patrimonio,n.marca,n.modelo,p.nome pessoa_nome,p.matricula,p.turma
                   FROM emprestimos e JOIN notebooks n ON e.notebook_id=n.id
                   JOIN pessoas p ON e.pessoa_id=p.id WHERE e.status='ativo'
                   ORDER BY e.retirada_em DESC LIMIT 10""")
    lr  =db_all("""SELECT l.*,p.nome pessoa_nome FROM logs l
                   LEFT JOIN pessoas p ON l.pessoa_id=p.id
                   ORDER BY l.criado_em DESC LIMIT 15""")
    ar  =db_all("""SELECT a.*,n.patrimonio,p.nome pessoa_nome FROM alertas a
                   LEFT JOIN notebooks n ON a.notebook_id=n.id
                   LEFT JOIN pessoas p ON a.pessoa_id=p.id
                   WHERE a.resolvido=0 ORDER BY a.criado_em DESC LIMIT 5""")
    chart=[]
    for i in range(6,-1,-1):
        d=(date.today()-timedelta(days=i)).isoformat()
        c=db_one("SELECT COUNT(*) as c FROM emprestimos WHERE date(retirada_em)=?",(d,))
        chart.append({"d":d[5:],"c":c["c"] if c else 0})
    return render_template("dashboard/index.html",
        t_nb=t_nb,disp=disp,emp=emp,t_p=t_p,eh=eh,al=al,
        ea=ea,lr=lr,ar=ar,chart=json.dumps(chart))

# ══════════════════════════════════════════════════════════
# NOTEBOOKS
# ══════════════════════════════════════════════════════════
@app.route("/notebooks")
@login_required
def notebooks():
    status=request.args.get("status",""); q=request.args.get("q","")
    sql="SELECT * FROM notebooks WHERE ativo=1"; params=[]
    if status: sql+=" AND status=?"; params.append(status)
    if q: sql+=" AND (patrimonio LIKE ? OR marca LIKE ? OR modelo LIKE ? OR serial LIKE ?)"; params+=[f"%{q}%"]*4
    return render_template("notebooks/list.html",notebooks=db_all(sql+" ORDER BY patrimonio",params),status=status,q=q)

@app.route("/notebooks/novo",methods=["GET","POST"])
@login_required
def notebook_novo():
    if request.method=="POST":
        pat=request.form.get("patrimonio","").strip().upper()
        if not pat: flash("Patrimônio obrigatório.","error"); return render_template("notebooks/form.html",nb=None)
        if db_one("SELECT id FROM notebooks WHERE patrimonio=?",(pat,)):
            flash("Patrimônio já cadastrado.","error"); return render_template("notebooks/form.html",nb=None)
        nid=new_id(); qrp=salvar_qrcode(nid,pat)
        db_exec("""INSERT INTO notebooks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                (nid,pat,request.form.get("serial","") or None,
                 request.form.get("marca",""),request.form.get("modelo",""),
                 request.form.get("processador",""),request.form.get("ram",""),
                 request.form.get("armazenamento",""),
                 int(request.form.get("ano",0) or 0) or None,
                 request.form.get("condicao","bom"),"disponivel",
                 request.form.get("localizacao","laboratorio"),qrp,
                 request.form.get("observacoes",""),now_str(),now_str()))
        log("notebook_cadastrado",f"Notebook {pat}",aid=session["admin_id"])
        flash(f"Notebook {pat} cadastrado com QR Code! ✅","success")
        return redirect(url_for("notebooks"))
    return render_template("notebooks/form.html",nb=None)

@app.route("/notebooks/<nid>")
@login_required
def notebook_detalhe(nid):
    nb=db_one("SELECT * FROM notebooks WHERE id=?",(nid,))
    if not nb: abort(404)
    hist=db_all("""SELECT e.*,p.nome pessoa_nome,p.matricula FROM emprestimos e
                   JOIN pessoas p ON e.pessoa_id=p.id WHERE e.notebook_id=?
                   ORDER BY e.retirada_em DESC LIMIT 20""",(nid,))
    qr=qrcode_b64(f"LUIZFABIANO|NB|{nid}|{nb['patrimonio']}")
    return render_template("notebooks/detalhe.html",nb=nb,historico=hist,qr_b64=qr)

@app.route("/notebooks/<nid>/editar",methods=["GET","POST"])
@login_required
def notebook_editar(nid):
    nb=db_one("SELECT * FROM notebooks WHERE id=?",(nid,))
    if not nb: abort(404)
    if request.method=="POST":
        db_exec("""UPDATE notebooks SET marca=?,modelo=?,processador=?,ram=?,
                   armazenamento=?,ano=?,condicao=?,localizacao=?,observacoes=?,atualizado_em=? WHERE id=?""",
                (request.form.get("marca",""),request.form.get("modelo",""),
                 request.form.get("processador",""),request.form.get("ram",""),
                 request.form.get("armazenamento",""),int(request.form.get("ano",0) or 0) or None,
                 request.form.get("condicao","bom"),request.form.get("localizacao","laboratorio"),
                 request.form.get("observacoes",""),now_str(),nid))
        flash("Notebook atualizado! ✅","success"); return redirect(url_for("notebook_detalhe",nid=nid))
    return render_template("notebooks/form.html",nb=nb)

@app.route("/notebooks/<nid>/qrcode")
@login_required
def notebook_qrcode(nid):
    nb=db_one("SELECT * FROM notebooks WHERE id=?",(nid,))
    if not nb: abort(404)
    img=gerar_qrcode(f"LUIZFABIANO|NB|{nid}|{nb['patrimonio']}",400)
    return Response(img,mimetype="image/png",headers={"Content-Disposition":f"attachment;filename=qr_{nb['patrimonio']}.png"})

@app.route("/notebooks/<nid>/desativar",methods=["POST"])
@nivel2
def notebook_desativar(nid):
    nb=db_one("SELECT * FROM notebooks WHERE id=?",(nid,))
    if not nb: abort(404)
    db_exec("UPDATE notebooks SET ativo=0,atualizado_em=? WHERE id=?",(now_str(),nid))
    flash(f"Notebook {nb['patrimonio']} desativado.","info"); return redirect(url_for("notebooks"))

# ══════════════════════════════════════════════════════════
# PESSOAS
# ══════════════════════════════════════════════════════════
@app.route("/pessoas")
@login_required
def pessoas():
    q=request.args.get("q",""); turma=request.args.get("turma","")
    sql="SELECT * FROM pessoas WHERE ativo=1"; params=[]
    if q: sql+=" AND (nome LIKE ? OR matricula LIKE ? OR email LIKE ?)"; params+=[f"%{q}%"]*3
    if turma: sql+=" AND turma=?"; params.append(turma)
    plist=db_all(sql+" ORDER BY nome",params)
    turmas=db_all("SELECT DISTINCT turma FROM pessoas WHERE ativo=1 AND turma IS NOT NULL ORDER BY turma")
    return render_template("pessoas/list.html",pessoas=plist,q=q,turma=turma,turmas=[t["turma"] for t in turmas])

@app.route("/pessoas/nova",methods=["GET","POST"])
@login_required
def pessoa_nova():
    if request.method=="POST":
        mat=request.form.get("matricula","").strip(); nome=request.form.get("nome","").strip()
        if not mat or not nome: flash("Nome e matrícula obrigatórios.","error"); return render_template("pessoas/form.html",pessoa=None)
        if db_one("SELECT id FROM pessoas WHERE matricula=?",(mat,)):
            flash("Matrícula já cadastrada.","error"); return render_template("pessoas/form.html",pessoa=None)
        pid=new_id()
        db_exec("""INSERT INTO pessoas VALUES(?,?,?,?,?,?,?,?,NULL,NULL,1,?,?)""",
                (pid,nome,mat,request.form.get("turma",""),request.form.get("curso",""),
                 request.form.get("email",""),request.form.get("telefone",""),
                 request.form.get("nivel_acesso","aluno"),now_str(),now_str()))
        log("pessoa_cadastrada",f"{nome} ({mat})",pid=pid,aid=session["admin_id"])
        flash(f"{nome} cadastrado! Agora capture o rosto. ✅","success")
        return redirect(url_for("pessoa_facial",pid=pid))
    return render_template("pessoas/form.html",pessoa=None)

@app.route("/pessoas/<pid>")
@login_required
def pessoa_detalhe(pid):
    p=db_one("SELECT * FROM pessoas WHERE id=?",(pid,))
    if not p: abort(404)
    hist=db_all("""SELECT e.*,n.patrimonio,n.marca,n.modelo FROM emprestimos e
                   JOIN notebooks n ON e.notebook_id=n.id WHERE e.pessoa_id=?
                   ORDER BY e.retirada_em DESC LIMIT 15""",(pid,))
    return render_template("pessoas/detalhe.html",pessoa=p,historico=hist)

@app.route("/pessoas/<pid>/editar",methods=["GET","POST"])
@login_required
def pessoa_editar(pid):
    p=db_one("SELECT * FROM pessoas WHERE id=?",(pid,))
    if not p: abort(404)
    if request.method=="POST":
        db_exec("UPDATE pessoas SET nome=?,turma=?,curso=?,email=?,telefone=?,nivel_acesso=?,atualizado_em=? WHERE id=?",
                (request.form.get("nome",""),request.form.get("turma",""),request.form.get("curso",""),
                 request.form.get("email",""),request.form.get("telefone",""),
                 request.form.get("nivel_acesso","aluno"),now_str(),pid))
        flash("Dados atualizados! ✅","success"); return redirect(url_for("pessoa_detalhe",pid=pid))
    return render_template("pessoas/form.html",pessoa=p)

@app.route("/pessoas/<pid>/facial")
@login_required
def pessoa_facial(pid):
    p=db_one("SELECT * FROM pessoas WHERE id=?",(pid,))
    if not p: abort(404)
    return render_template("pessoas/facial.html",pessoa=p)

@app.route("/pessoas/<pid>/desativar",methods=["POST"])
@nivel2
def pessoa_desativar(pid):
    p=db_one("SELECT * FROM pessoas WHERE id=?",(pid,))
    if not p: abort(404)
    db_exec("UPDATE pessoas SET ativo=0,atualizado_em=? WHERE id=?",(now_str(),pid))
    flash(f"{p['nome']} desativado.","info"); return redirect(url_for("pessoas"))

# ══════════════════════════════════════════════════════════
# EMPRÉSTIMOS
# ══════════════════════════════════════════════════════════
@app.route("/emprestimos")
@login_required
def emprestimos():
    status=request.args.get("status","ativo"); page=max(int(request.args.get("page","1")),1)
    per=20; off=(page-1)*per
    sql="""SELECT e.*,n.patrimonio,n.marca,n.modelo,p.nome pessoa_nome,p.matricula,p.turma
           FROM emprestimos e JOIN notebooks n ON e.notebook_id=n.id
           JOIN pessoas p ON e.pessoa_id=p.id"""
    params=[]; wh=""
    if status: wh=" WHERE e.status=?"; params.append(status)
    total=db_one("SELECT COUNT(*) as c FROM emprestimos e"+wh,params)["c"]
    emps=db_all(sql+wh+f" ORDER BY e.retirada_em DESC LIMIT {per} OFFSET {off}",params)
    return render_template("emprestimos/list.html",emprestimos=emps,status=status,
                           page=page,total=total,pages=max(1,(total+per-1)//per))

@app.route("/emprestimos/novo",methods=["GET","POST"])
@login_required
def emprestimo_novo():
    if request.method=="POST":
        nid=request.form.get("notebook_id"); pid=request.form.get("pessoa_id")
        horas=int(request.form.get("horas",4) or 4)
        if not nid or not pid:
            flash("Selecione notebook e pessoa.","error")
            return render_template("emprestimos/form.html",
                notebooks=db_all("SELECT * FROM notebooks WHERE status='disponivel' AND ativo=1 ORDER BY patrimonio"),
                pessoas=db_all("SELECT * FROM pessoas WHERE ativo=1 ORDER BY nome"))
        nb=db_one("SELECT * FROM notebooks WHERE id=? AND status='disponivel'",(nid,))
        if not nb: flash("Notebook não disponível.","error"); return redirect(url_for("emprestimo_novo"))
        eid=new_id(); dev=(datetime.now()+timedelta(hours=horas)).strftime("%Y-%m-%d %H:%M:%S")
        db_exec("INSERT INTO emprestimos VALUES(?,?,?,?,'manual','ativo',?,?,NULL,NULL,?)",
                (eid,nid,pid,session["admin_id"],now_str(),dev,request.remote_addr))
        db_exec("UPDATE notebooks SET status='emprestado',atualizado_em=? WHERE id=?",(now_str(),nid))
        p=db_one("SELECT nome,matricula FROM pessoas WHERE id=?",(pid,))
        log("emprestimo",f"Retirada: {nb['patrimonio']} -> {p['nome']}",pid=pid,nid=nid,aid=session["admin_id"])
        telegram_send(f"📤 <b>Retirada</b>\n{nb['patrimonio']} -> {p['nome']} ({p['matricula']})\nDev: {dev}")
        flash(f"Empréstimo registrado! {nb['patrimonio']} → {p['nome']} ✅","success")
        return redirect(url_for("emprestimos"))
    return render_template("emprestimos/form.html",
        notebooks=db_all("SELECT * FROM notebooks WHERE status='disponivel' AND ativo=1 ORDER BY patrimonio"),
        pessoas=db_all("SELECT * FROM pessoas WHERE ativo=1 ORDER BY nome"))

@app.route("/emprestimos/<eid>/devolver",methods=["POST"])
@login_required
def emprestimo_devolver(eid):
    emp=db_one("SELECT * FROM emprestimos WHERE id=? AND status='ativo'",(eid,))
    if not emp: abort(404)
    db_exec("UPDATE emprestimos SET status='devolvido',devolucao_em=? WHERE id=?",(now_str(),eid))
    db_exec("UPDATE notebooks SET status='disponivel',atualizado_em=? WHERE id=?",(now_str(),emp["notebook_id"]))
    nb=db_one("SELECT patrimonio FROM notebooks WHERE id=?",(emp["notebook_id"],))
    p=db_one("SELECT nome FROM pessoas WHERE id=?",(emp["pessoa_id"],))
    log("devolucao",f"Devolvido: {nb['patrimonio']}",pid=emp["pessoa_id"],nid=emp["notebook_id"],aid=session["admin_id"])
    telegram_send(f"📥 <b>Devolução</b>\n{nb['patrimonio']} <- {p['nome']}")
    flash(f"Devolução registrada! ✅","success"); return redirect(url_for("emprestimos"))

# ══════════════════════════════════════════════════════════
# ALARME
# ══════════════════════════════════════════════════════════
@app.route("/alarme")
@login_required
def alarme_painel():
    alertas=db_all("""SELECT a.*,n.patrimonio,p.nome pessoa_nome FROM alertas a
                      LEFT JOIN notebooks n ON a.notebook_id=n.id
                      LEFT JOIN pessoas p ON a.pessoa_id=p.id
                      ORDER BY a.criado_em DESC LIMIT 50""")
    return render_template("alarme/painel.html",alertas=alertas)

@app.route("/alarme/ativar",methods=["POST"])
@login_required
def alarme_ativar():
    app.config["ALARME_ATIVO"]=True
    log("alarme_ativado","Alarme ativado manualmente",aid=session["admin_id"])
    alerta("alarme_manual",f"Alarme ativado por {session['admin_nome']}")
    flash("🚨 ALARME ATIVADO!","error"); return redirect(url_for("alarme_painel"))

@app.route("/alarme/desativar",methods=["POST"])
@login_required
def alarme_desativar():
    app.config["ALARME_ATIVO"]=False
    log("alarme_desativado","Alarme desativado",aid=session["admin_id"])
    flash("✅ Alarme desativado.","success"); return redirect(url_for("alarme_painel"))

@app.route("/alarme/<aid>/resolver",methods=["POST"])
@login_required
def alerta_resolver(aid):
    db_exec("UPDATE alertas SET resolvido=1,resolvido_por=?,resolvido_em=? WHERE id=?",(session["admin_id"],now_str(),aid))
    flash("Alerta resolvido. ✅","success"); return redirect(url_for("alarme_painel"))

# ══════════════════════════════════════════════════════════
# LOGS
# ══════════════════════════════════════════════════════════
@app.route("/logs")
@login_required
def logs_view():
    tipo=request.args.get("tipo",""); data=request.args.get("data","")
    page=max(int(request.args.get("page","1")),1); per=30; off=(page-1)*per
    sql="""SELECT l.*,p.nome pessoa_nome,a.nome admin_nome FROM logs l
           LEFT JOIN pessoas p ON l.pessoa_id=p.id LEFT JOIN admins a ON l.admin_id=a.id WHERE 1=1"""
    params=[]
    if tipo: sql+=" AND l.tipo=?"; params.append(tipo)
    if data: sql+=" AND date(l.criado_em)=?"; params.append(data)
    cnt_sql="SELECT COUNT(*) as c FROM logs WHERE 1=1"
    cnt_params=[]
    if tipo: cnt_sql+=" AND tipo=?"; cnt_params.append(tipo)
    if data: cnt_sql+=" AND date(criado_em)=?"; cnt_params.append(data)
    total=db_one(cnt_sql,cnt_params)["c"]
    ls=db_all(sql+f" ORDER BY l.criado_em DESC LIMIT {per} OFFSET {off}",params)
    tipos=db_all("SELECT DISTINCT tipo FROM logs ORDER BY tipo")
    return render_template("logs/list.html",logs=ls,tipo=tipo,data=data,
                           page=page,total=total,pages=max(1,(total+per-1)//per),
                           tipos=[t["tipo"] for t in tipos])

# ══════════════════════════════════════════════════════════
# ADMIN
# ══════════════════════════════════════════════════════════
@app.route("/admin")
@nivel2
def admin_panel():
    admins=db_all("SELECT * FROM admins ORDER BY nome")
    configs=db_all("SELECT * FROM configuracoes ORDER BY chave")
    return render_template("admin/painel.html",admins=admins,configs=configs)

@app.route("/admin/novo",methods=["GET","POST"])
@nivel2
def admin_novo():
    if request.method=="POST":
        email=request.form.get("email","").lower().strip()
        nome=request.form.get("nome","").strip()
        senha=request.form.get("senha","")
        nivel=int(request.form.get("nivel",1) or 1)
        if not email or not nome or len(senha)<8:
            flash("Preencha tudo. Senha mín. 8 chars.","error")
            return render_template("admin/form.html",adm=None)
        if db_one("SELECT id FROM admins WHERE email=?",(email,)):
            flash("Email já cadastrado.","error"); return render_template("admin/form.html",adm=None)
        db_exec("INSERT INTO admins VALUES(?,?,?,?,?,1,?)",(new_id(),nome,email,_hash_pw(senha),nivel,now_str()))
        flash(f"Admin {nome} criado! ✅","success"); return redirect(url_for("admin_panel"))
    return render_template("admin/form.html",adm=None)

@app.route("/admin/config",methods=["POST"])
@nivel2
def admin_config_save():
    for k,v in request.form.items():
        if k.startswith("cfg_"):
            db_exec("UPDATE configuracoes SET valor=?,atualizado_em=? WHERE chave=?",(v.strip(),now_str(),k[4:]))
    flash("Configurações salvas! ✅","success"); return redirect(url_for("admin_panel"))

# ══════════════════════════════════════════════════════════
# API
# ══════════════════════════════════════════════════════════
@app.route("/api/status")
def api_status():
    return jsonify({
        "alarme_ativo":app.config["ALARME_ATIVO"],
        "porta_aberta":app.config["PORTA_ABERTA"],
        "notebooks_disponiveis":db_one("SELECT COUNT(*) as c FROM notebooks WHERE status='disponivel' AND ativo=1")["c"],
        "emprestimos_ativos":db_one("SELECT COUNT(*) as c FROM emprestimos WHERE status='ativo'")["c"],
        "alertas_abertos":db_one("SELECT COUNT(*) as c FROM alertas WHERE resolvido=0")["c"],
        "timestamp":now_str()})

@app.route("/api/qrscan",methods=["POST"])
def api_qrscan():
    data=request.json or {}; qr=data.get("qr_data",""); pid=data.get("pessoa_id","")
    if not qr.startswith("LUIZFABIANO|NB|"): return jsonify({"ok":False,"msg":"QR inválido."})
    parts=qr.split("|")
    if len(parts)<4: return jsonify({"ok":False,"msg":"QR mal formatado."})
    nid=parts[2]; pat=parts[3]
    nb=db_one("SELECT * FROM notebooks WHERE id=? AND ativo=1",(nid,)) or db_one("SELECT * FROM notebooks WHERE patrimonio=? AND ativo=1",(pat,))
    if not nb: return jsonify({"ok":False,"msg":f"Notebook {pat} não encontrado."})
    emp=db_one("SELECT * FROM emprestimos WHERE notebook_id=? AND status='ativo'",(nb["id"],))
    if emp:
        db_exec("UPDATE emprestimos SET status='devolvido',devolucao_em=? WHERE id=?",(now_str(),emp["id"]))
        db_exec("UPDATE notebooks SET status='disponivel',atualizado_em=? WHERE id=?",(now_str(),nb["id"]))
        log("devolucao_qr",f"Devolução QR: {pat}",pid=emp["pessoa_id"],nid=nb["id"])
        return jsonify({"ok":True,"acao":"devolucao","msg":f"✅ {pat} devolvido com sucesso!"})
    else:
        if not pid: return jsonify({"ok":False,"msg":"Identifique-se primeiro.","requer_id":True})
        cfg=db_one("SELECT valor FROM configuracoes WHERE chave='tempo_emprestimo_horas'")
        horas=int(cfg["valor"] if cfg else 4)
        dev=(datetime.now()+timedelta(hours=horas)).strftime("%Y-%m-%d %H:%M:%S")
        eid=new_id()
        db_exec("INSERT INTO emprestimos VALUES(?,?,?,'qrcode_auto','qrcode','ativo',?,?,NULL,NULL,?)",
                (eid,nb["id"],pid,now_str(),dev,request.remote_addr))
        db_exec("UPDATE notebooks SET status='emprestado',atualizado_em=? WHERE id=?",(now_str(),nb["id"]))
        p=db_one("SELECT nome,matricula FROM pessoas WHERE id=?",(pid,))
        log("retirada_qr",f"Retirada QR: {pat}",pid=pid,nid=nb["id"])
        telegram_send(f"📤 <b>Retirada QR</b>\n{pat} -> {p['nome'] if p else pid}\nDev: {dev}")
        return jsonify({"ok":True,"acao":"retirada","msg":f"✅ {pat} retirado! Dev: {dev[:16]}","devolucao":dev})

@app.route("/api/reconhecer",methods=["POST"])
def api_reconhecer():
    data=request.json or {}; ib=data.get("imagem","")
    if not ib: return jsonify({"ok":False,"msg":"Imagem não recebida."})
    try:
        nb=np.frombuffer(base64.b64decode(ib.split(",")[-1]),np.uint8)
        frame=cv2.imdecode(nb,cv2.IMREAD_COLOR)
        if frame is None: return jsonify({"ok":False,"msg":"Imagem inválida."})
        cfg=db_one("SELECT valor FROM configuracoes WHERE chave='facial_threshold'")
        thr=float(cfg["valor"] if cfg else 0.55)
        pessoa,conf=reconhecer(frame,thr)
        if pessoa:
            log("facial_reconhecido",f"Reconhecido: {pessoa['nome']} ({conf:.2f})",pid=pessoa["id"])
            return jsonify({"ok":True,"reconhecido":True,"pessoa_id":pessoa["id"],"nome":pessoa["nome"],
                            "matricula":pessoa["matricula"],"turma":pessoa.get("turma",""),"confianca":round(conf*100,1)})
        return jsonify({"ok":True,"reconhecido":False,"confianca":round(conf*100,1),"msg":"Não reconhecido."})
    except Exception as e: return jsonify({"ok":False,"msg":str(e)[:80]})

@app.route("/api/cadastrar_facial",methods=["POST"])
@login_required
def api_cadastrar_facial():
    data=request.json or {}; pid=data.get("pessoa_id",""); ib=data.get("imagem","")
    if not pid or not ib: return jsonify({"ok":False,"msg":"Dados incompletos."})
    try:
        nb=np.frombuffer(base64.b64decode(ib.split(",")[-1]),np.uint8)
        frame=cv2.imdecode(nb,cv2.IMREAD_COLOR)
        if frame is None: return jsonify({"ok":False,"msg":"Imagem inválida."})
        faces=detectar_rostos(frame)
        if len(faces)==0: return jsonify({"ok":False,"msg":"Nenhum rosto detectado. Ajuste o posicionamento."})
        enc=extrair_enc(frame,faces[0])
        p=db_one("SELECT nome FROM pessoas WHERE id=?",(pid,))
        fn=f"face_{pid[:8]}.jpg"; fp=os.path.join(app.config["FACES_DIR"],fn)
        x,y,w,h=faces[0]; roi=frame[max(0,y-20):y+h+20,max(0,x-20):x+w+20]
        cv2.imwrite(fp,roi)
        db_exec("UPDATE pessoas SET face_encoding=?,foto_path=?,atualizado_em=? WHERE id=?",
                (json.dumps(enc),f"static/faces/{fn}",now_str(),pid))
        log("facial_cadastrado",f"Rosto: {p['nome']}",pid=pid,aid=session["admin_id"])
        return jsonify({"ok":True,"msg":f"✅ Rosto de {p['nome']} cadastrado!"})
    except Exception as e: return jsonify({"ok":False,"msg":str(e)[:80]})

@app.route("/api/porta",methods=["POST"])
def api_porta():
    data=request.json or {}
    if data.get("token","")!=os.environ.get("HARDWARE_TOKEN","luizfabiano-hw-token"):
        return jsonify({"ok":False}),401
    app.config["PORTA_ABERTA"]=data.get("aberta",False)
    if data.get("aberta"):
        emp=db_one("SELECT COUNT(*) as c FROM emprestimos WHERE status='ativo'")["c"]
        if emp>0 and app.config["ALARME_ATIVO"]:
            alerta("porta_alarme",f"Porta aberta com {emp} notebooks emprestados!")
        log("porta_aberta","Sensor: porta aberta")
    return jsonify({"ok":True,"alarme":app.config["ALARME_ATIVO"]})

@app.route("/api/stream_status")
def stream_status():
    def gen():
        last=""
        while True:
            try:
                ea=db_one("SELECT COUNT(*) as c FROM emprestimos WHERE status='ativo'")
                al=db_one("SELECT COUNT(*) as c FROM alertas WHERE resolvido=0")
                s=json.dumps({"alarme":app.config["ALARME_ATIVO"],"porta":app.config["PORTA_ABERTA"],
                              "emp":ea["c"] if ea else 0,"alertas":al["c"] if al else 0,"ts":now_str()})
                if s!=last: yield f"data: {s}\n\n"; last=s
            except: pass
            time.sleep(3)
    return Response(gen(),mimetype="text/event-stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

# ── TOTEM ─────────────────────────────────────────────────
@app.route("/totem")
def totem():
    disp=db_one("SELECT COUNT(*) as c FROM notebooks WHERE status='disponivel' AND ativo=1")["c"]
    return render_template("totem/index.html",disp=disp)

# ══════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════
with app.app_context():
    init_db()

if __name__=="__main__":
    print("="*55)
    print("  luizFabiano — Controle Inteligente de Notebooks")
    print("="*55)
    print("  Admin:  admin@luizfabiano.com / admin123456")
    print("  Totem:  http://localhost:5000/totem")
    print("  Painel: http://localhost:5000/dashboard")
    print("="*55)
    app.run(debug=True,host="0.0.0.0",port=5000,threaded=True)
