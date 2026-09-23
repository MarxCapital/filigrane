import io
import re
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


# =============================================================================
# Interface Streamlit
# =============================================================================

st.set_page_config(page_title="Nettoyeur de fiches", page_icon="📄")
st.title("📄 Personnalisation de fiches (filigrane / pied de page)")
st.write(
    "Depose ton PDF, indique le nom du nouveau client, et recupere le fichier "
    "avec le filigrane / pied de page remplace par le nouveau nom. "
    "L'outil detecte automatiquement le format du filigrane (texte direct "
    "en pied de page, ou filigrane diagonal via objet forme)."
)

uploaded_file = st.file_uploader("Fichier PDF", type=["pdf"])
new_name = st.text_input("Nom a afficher (ex : TOHOUINDJI E. Alice)")
add_footer = st.checkbox(
    "Ajouter aussi une bande 'Realisee et concue par ...' en bas de chaque page",
    value=True,
)

if st.button("Generer", type="primary"):
    if uploaded_file is None:
        st.error("Merci d'ajouter un fichier PDF.")
    elif not new_name.strip():
        st.error("Merci de saisir un nom.")
    else:
        with st.spinner("Traitement en cours..."):
            try:
                pdf_bytes = uploaded_file.read()
                final_bytes, pages_without_marker = process_pdf(
                    pdf_bytes, new_name.strip(), add_footer
                )
            except Exception as e:
                st.error(f"Une erreur est survenue : {e}")
            else:
                if pages_without_marker:
                    st.warning(
                        "Aucun filigrane reconnu sur la ou les page(s) : "
                        f"{', '.join(map(str, pages_without_marker))}. "
                        "Ces pages ont ete laissees telles quelles (seule la "
                        "bande de pied de page, si activee, a ete ajoutee)."
                    )
                st.success("Termine !")
                base_name = uploaded_file.name.rsplit(".", 1)[0]
                out_filename = f"{base_name}_{new_name.strip().replace(' ', '_')}.pdf"
                st.download_button(
                    "⬇️ Telecharger le PDF final",
                    data=final_bytes,
                    file_name=out_filename,
                    mime="application/pdf",
                )
