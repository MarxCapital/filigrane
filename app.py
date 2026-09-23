import io
import re
import zipfile
import streamlit as st
import pikepdf
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.colors import white, black

# =============================================================================
# DETECTION GENERIQUE DES ARTEFACTS DE PAGINATION
# (pied de page / en-tete / filigrane), quel que soit leur format d'origine
# =============================================================================
#
# Les logiciels d'export PDF (Word, LibreOffice, InDesign, etc.) marquent le
# pied de page / en-tete / filigrane comme un bloc "Artifact" balise par
#   /Artifact <<...>> BDC   ...   EMC
# Le dictionnaire contient /Type /Pagination et un /Subtype parmi
# /Footer, /Header ou /Watermark. Deux variantes existent en pratique :
#
#   (a) TEXTE DIRECT : le nom est ecrit en clair dans le bloc (operateurs
#       Tj / TJ), comme dans les anciennes fiches (pied de page en bas).
#
#   (b) VIA XObject : le bloc ne fait qu'appeler un objet forme externe
#       (ex: "/Fm1 Do"), et c'est CET objet qui contient le texte (souvent
#       en diagonal, avec sa propre matrice de rotation "cm"). C'est le cas
#       du filigrane diagonal (ex: "BOUKARY D. K. Moustapha").
#
# Le code ci-dessous detecte les deux variantes sur chaque page et remplace
# le nom du client precedent par le nouveau, en conservant la position, la
# police et la rotation d'origine (on ne fait que substituer le texte).

ARTIFACT_RE = re.compile(
    rb"/Artifact\s*<<(?P<dict>[^>]*)>>\s*BDC(?P<body>.*?)EMC",
    re.DOTALL,
)
XOBJECT_DO_RE = re.compile(rb"/(?P<name>[A-Za-z0-9_.+#-]+)\s+Do")
TEXT_SHOW_RE = re.compile(rb"Tj|TJ")

# Types de pagination reconnus : pied de page, en-tete, filigrane
PAGINATION_SUBTYPES = (b"/Footer", b"/Header", b"/Watermark")


def is_pagination_artifact(dict_bytes: bytes) -> bool:
    return b"/Pagination" in dict_bytes and any(
        s in dict_bytes for s in PAGINATION_SUBTYPES
    )


def escape_pdf_string(text: str) -> bytes:
    """Encode une chaine Python en litteral PDF (parentheses/backslash echappes)."""
    raw = text.encode("latin-1", errors="replace")
    raw = raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    return raw


def rewrite_text_in_stream(data: bytes, new_text: str):
    """Remplace le premier bloc de texte (BT ... premier Tj/TJ ... ET) d'un flux
    de contenu PDF par un texte unique, en conservant tout ce qui precede
    (police Tf, couleur, position Td/Tm, matrice cm, etc.).
    Retourne (nouveau_flux, trouve: bool).
    """
    m = re.search(
        rb"BT(?P<pre>.*?)(?:\((?:\\.|[^\\()])*\)\s*Tj|\[[^\]]*\]\s*TJ).*?ET",
        data,
        re.DOTALL,
    )
    if not m:
        return data, False
    preamble = m.group("pre")
    replacement = b"BT" + preamble + b"(" + escape_pdf_string(new_text) + b") Tj ET"
    new_data = data[: m.start()] + replacement + data[m.end():]
    return new_data, True


def process_page_artifacts(page: pikepdf.Page, new_text: str):
    """Traite tous les artefacts de pagination d'une page : remplace le texte
    (direct ou via XObject). Retourne True si au moins un artefact a ete traite."""
    # Fusionne les flux de contenu multiples en un seul, sinon on ne peut pas
    # analyser/editer le flux de la page de facon fiable.
    page.contents_coalesce()
    contents = page.obj["/Contents"]
    data = contents.read_bytes()

    resources = page.obj.get("/Resources")
    xobjects = None
    if resources is not None and "/XObject" in resources:
        xobjects = resources["/XObject"]

    matches = list(ARTIFACT_RE.finditer(data))
    if not matches:
        return False

    handled = False
    new_data = data
    # On parcourt les correspondances en partant de la fin du flux pour ne
    # pas decaler les positions des correspondances precedentes lors des
    # suppressions de blocs.
    for m in reversed(matches):
        if not is_pagination_artifact(m.group("dict")):
            continue
        body = m.group("body")

        if TEXT_SHOW_RE.search(body):
            # (a) Texte ecrit directement dans le flux de la page -> on
            # retire completement ce bloc (nom + eventuels traits/encadres
            # associes dans le meme bloc marque).
            new_data = new_data[: m.start()] + new_data[m.end():]
            handled = True
            continue

        # (b) Pas de texte direct : on cherche un appel a un objet forme
        # (ex: "/Fm1 Do") et on remplace le texte a l'interieur de CET objet,
        # en gardant la matrice de position/rotation du flux de page intacte.
        do_match = XOBJECT_DO_RE.search(body)
        if do_match and xobjects is not None:
            xobj_name = "/" + do_match.group("name").decode("latin-1")
            if xobj_name in xobjects:
                xobj = xobjects[xobj_name]
                xdata = xobj.read_bytes()
                xnew, ok = rewrite_text_in_stream(xdata, new_text)
                if ok:
                    xobj.write(xnew)
                    handled = True

    if new_data != data:
        contents.write(new_data)

    return handled


def personalize_artifacts(pdf_bytes: bytes, new_name: str):
    """Parcourt toutes les pages et personnalise tous les filigranes /
    pieds de page / en-tetes detectes (texte direct ou via XObject)."""
    pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    pages_without_marker = []
    for i, page in enumerate(pdf.pages):
        found = process_page_artifacts(page, new_name)
        if not found:
            pages_without_marker.append(i + 1)
    out = io.BytesIO()
    pdf.save(out)
    out.seek(0)
    return out.read(), pages_without_marker


# =============================================================================
# PIED DE PAGE CALIBRE (gabarit A4) : ajoute "Realisee et concue par ..."
# en bas de chaque page, quel que soit son contenu d'origine.
# =============================================================================

REF_PAGE_W = 595.32
REF_PAGE_H = 841.92
FOOTER_TOP_FROM_TOP = 795.216
FOOTER_BOTTOM_FROM_TOP = 806.256


def build_overlay(page_w: float, page_h: float, new_text: str) -> bytes:
    """Construit un calque PDF (1 page) : rectangle blanc + nouveau texte,
    positionne proportionnellement a la taille reelle de la page."""
    scale_x = page_w / REF_PAGE_W
    scale_y = page_h / REF_PAGE_H

    rect_x0 = 40 * scale_x
    rect_x1 = page_w - 40 * scale_x
    rect_y0 = page_h - (FOOTER_BOTTOM_FROM_TOP * scale_y) - 3
    rect_y1 = page_h - (FOOTER_TOP_FROM_TOP * scale_y) + 3

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.setFillColor(white)
    c.rect(rect_x0, rect_y0, rect_x1 - rect_x0, rect_y1 - rect_y0, fill=1, stroke=0)

    c.setFillColor(black)
    c.setFont("Helvetica", 11)
    tw = c.stringWidth(new_text, "Helvetica", 11)
    baseline_y = page_h - (FOOTER_BOTTOM_FROM_TOP * scale_y) + 2.2
    c.drawString((page_w - tw) / 2, baseline_y, new_text)
    c.save()
    buf.seek(0)
    return buf.read()


def add_footer_band(pdf_bytes: bytes, new_name: str) -> bytes:
    new_text = f"Réalisée et conçue par {new_name}"
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()

    overlay_cache = {}
    for page in reader.pages:
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)
        key = (round(w, 1), round(h, 1))
        if key not in overlay_cache:
            overlay_bytes = build_overlay(w, h, new_text)
            overlay_cache[key] = PdfReader(io.BytesIO(overlay_bytes)).pages[0]
        page.merge_page(overlay_cache[key])
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()


def process_pdf(pdf_bytes: bytes, new_name: str, add_footer: bool):
    personalized_bytes, pages_without_marker = personalize_artifacts(pdf_bytes, new_name)
    final_bytes = add_footer_band(personalized_bytes, new_name) if add_footer else personalized_bytes
    return final_bytes, pages_without_marker


def make_zip(files: list[tuple[str, bytes]]) -> bytes:
    """Construit une archive .zip en memoire a partir d'une liste (nom, contenu)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files:
            zf.writestr(name, data)
    buf.seek(0)
    return buf.read()


# =============================================================================
# Interface Streamlit
# =============================================================================

st.set_page_config(
    page_title="Nettoyeur de fiches",
    page_icon="📄",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
        :root {
            --bleu-fonce: #0B3D91;
            --bleu: #1E6FD9;
            --bleu-clair: #EAF2FE;
            --bleu-bordure: #C7DCFB;
        }

        .stApp {
            background: linear-gradient(180deg, #F3F8FF 0%, #FFFFFF 320px);
        }

        /* Bandeau d'entete */
        .app-header {
            background: linear-gradient(120deg, var(--bleu-fonce), var(--bleu));
            padding: 2rem 2rem 1.6rem 2rem;
            border-radius: 18px;
            color: #FFFFFF;
            margin-bottom: 1.8rem;
            box-shadow: 0 8px 24px rgba(11, 61, 145, 0.18);
        }
        .app-header h1 {
            color: #FFFFFF;
            font-size: 1.6rem;
            margin: 0 0 0.4rem 0;
        }
        .app-header p {
            color: #DCE9FF;
            margin: 0;
            font-size: 0.95rem;
        }

        /* Cartes de section */
        .section-card {
            background: #FFFFFF;
            border: 1px solid var(--bleu-bordure);
            border-radius: 14px;
            padding: 1.4rem 1.5rem;
            margin-bottom: 1.2rem;
            box-shadow: 0 2px 10px rgba(11, 61, 145, 0.05);
        }
        .section-card h3 {
            color: var(--bleu-fonce);
            font-size: 1.05rem;
            margin-top: 0;
            margin-bottom: 0.8rem;
        }

        /* Champs */
        .stTextInput input, .stFileUploader, div[data-testid="stFileUploaderDropzone"] {
            border-radius: 10px !important;
        }
        div[data-testid="stFileUploaderDropzone"] {
            background-color: var(--bleu-clair) !important;
            border: 1.5px dashed var(--bleu-bordure) !important;
        }

        /* Bouton principal */
        .stButton > button[kind="primary"] {
            background-color: var(--bleu);
            border: none;
            border-radius: 10px;
            padding: 0.55rem 1.4rem;
            font-weight: 600;
            box-shadow: 0 4px 14px rgba(30, 111, 217, 0.3);
        }
        .stButton > button[kind="primary"]:hover {
            background-color: var(--bleu-fonce);
        }

        /* Boutons de telechargement */
        .stDownloadButton > button {
            background-color: #FFFFFF;
            color: var(--bleu-fonce);
            border: 1.5px solid var(--bleu);
            border-radius: 10px;
            font-weight: 600;
        }
        .stDownloadButton > button:hover {
            background-color: var(--bleu-clair);
        }

        /* Conteneurs des resultats par fichier */
        div[data-testid="stVerticalBlockBorderWrapper"] {
            border-radius: 12px !important;
            border-color: var(--bleu-bordure) !important;
            background-color: #FBFDFF;
        }

        .stCaption, .stMarkdown small {
            color: #4A6D9C;
        }
    </style>

    <div class="app-header">
        <h1>📄 Personnalisation de fiches pedagogiques</h1>
        <p>Detection et remplacement automatique du filigrane / pied de page (nom du client) sur un ou plusieurs PDF.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="section-card">', unsafe_allow_html=True)
st.markdown("### 1. Fiches à traiter")
uploaded_files = st.file_uploader(
    "Depose ici un ou plusieurs fichiers PDF",
    type=["pdf"],
    accept_multiple_files=True,
    label_visibility="collapsed",
)
if uploaded_files:
    st.caption(f"✅ {len(uploaded_files)} fichier(s) selectionne(s).")
st.markdown("</div>", unsafe_allow_html=True)

st.markdown('<div class="section-card">', unsafe_allow_html=True)
st.markdown("### 2. Nom du nouveau client")
new_name = st.text_input(
    "Nom a afficher",
    placeholder="ex : TOHOUINDJI E. Alice",
    label_visibility="collapsed",
)
add_footer = st.checkbox(
    "Ajouter aussi une bande « Réalisée et conçue par ... » en bas de chaque page",
    value=True,
)
st.markdown("</div>", unsafe_allow_html=True)

col_btn = st.columns([1, 1, 1])[1]
with col_btn:
    lancer = st.button("✨ Générer", type="primary", use_container_width=True)

if lancer:
    if not uploaded_files:
        st.error("Merci d'ajouter au moins un fichier PDF.")
    elif not new_name.strip():
        st.error("Merci de saisir un nom.")
    else:
        results = []  # (nom_fichier, bytes, pages_sans_marqueur, erreur)
        progress = st.progress(0.0, text="Traitement en cours...")
        for i, uploaded_file in enumerate(uploaded_files):
            progress.progress(
                i / len(uploaded_files),
                text=f"Traitement de {uploaded_file.name} ({i + 1}/{len(uploaded_files)})...",
            )
            try:
                pdf_bytes = uploaded_file.read()
                final_bytes, pages_without_marker = process_pdf(
                    pdf_bytes, new_name.strip(), add_footer
                )
            except Exception as e:
                results.append((uploaded_file.name, None, None, str(e)))
            else:
                base_name = uploaded_file.name.rsplit(".", 1)[0]
                out_filename = f"{base_name}_{new_name.strip().replace(' ', '_')}.pdf"
                results.append((out_filename, final_bytes, pages_without_marker, None))
        progress.progress(1.0, text="Termine !")
        progress.empty()

        n_ok = sum(1 for r in results if r[3] is None)
        n_err = sum(1 for r in results if r[3] is not None)
        if n_err:
            st.warning(f"{n_ok} fichier(s) traite(s), {n_err} en erreur.")
        else:
            st.success(f"{n_ok} fichier(s) traite(s) avec succes !")

        # Telechargement groupe (.zip) si plusieurs fichiers ont reussi
        ok_files = [(name, data) for name, data, _, err in results if err is None]
        if len(ok_files) > 1:
            zip_bytes = make_zip(ok_files)
            st.download_button(
                "⬇️ Telecharger tous les fichiers (.zip)",
                data=zip_bytes,
                file_name=f"fiches_{new_name.strip().replace(' ', '_')}.zip",
                mime="application/zip",
            )

        st.markdown("### 3. Résultats")

        # Detail + telechargement individuel pour chaque fichier
        for out_filename, final_bytes, pages_without_marker, error in results:
            if error is not None:
                st.error(f"❌ {out_filename} : {error}")
                continue

            with st.container(border=True):
                st.write(f"**{out_filename}**")
                if pages_without_marker:
                    st.warning(
                        "Aucun filigrane reconnu sur la ou les page(s) : "
                        f"{', '.join(map(str, pages_without_marker))}. "
                        "Ces pages ont ete laissees telles quelles (seule la "
                        "bande de pied de page, si activee, a ete ajoutee)."
                    )
                else:
                    st.caption("Filigrane detecte et remplace sur toutes les pages.")
                st.download_button(
                    "⬇️ Telecharger ce PDF",
                    data=final_bytes,
                    file_name=out_filename,
                    mime="application/pdf",
                    key=f"dl_{out_filename}",
                )
