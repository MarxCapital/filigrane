import io
import streamlit as st
import pikepdf
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.colors import white, black

FOOTER_MARKER = b"/Artifact <</Attached [/Bottom]/Type/Pagination/Subtype/Footer>> BDC"

# Position standard du pied de page, mesurée sur les fiches de référence
# (page A4 : 595.32 x 841.92 pt ; y mesuré depuis le HAUT de la page)
REF_PAGE_W = 595.32
REF_PAGE_H = 841.92
FOOTER_TOP_FROM_TOP = 795.216   # haut du texte du pied de page
FOOTER_BOTTOM_FROM_TOP = 806.256  # bas du texte du pied de page


def strip_watermark(pdf_bytes: bytes):
    """Supprime le bloc /Artifact ... Header (filigrane) de chaque page."""
    pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    pages_without_marker = []
    for i, page in enumerate(pdf.pages):
        contents = page.get("/Contents")
        if contents is None or isinstance(contents, pikepdf.Array):
            pages_without_marker.append(i + 1)
            continue
        data = contents.read_bytes()
        idx = data.find(FOOTER_MARKER)
        if idx == -1:
            pages_without_marker.append(i + 1)
            continue
        contents.write(data[idx:])
    out = io.BytesIO()
    pdf.save(out)
    out.seek(0)
    return out.read(), pages_without_marker


def build_overlay(page_w: float, page_h: float, new_text: str) -> bytes:
    """Construit un calque PDF (1 page) : rectangle blanc + nouveau texte,
    positionné proportionnellement à la taille réelle de la page."""
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


def replace_footer(pdf_bytes: bytes, new_name: str) -> bytes:
    new_text = f"Réalisée et conçu par {new_name}"
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


def process_pdf(pdf_bytes: bytes, new_name: str):
    stripped_bytes, pages_without_marker = strip_watermark(pdf_bytes)
    final_bytes = replace_footer(stripped_bytes, new_name)
    return final_bytes, pages_without_marker


# ---------------- Interface Streamlit ----------------

st.set_page_config(page_title="Nettoyeur de fiches", page_icon="📄")
st.title("📄 Nettoyeur de filigrane pour fiches")
st.write(
    "Dépose ton PDF, indique le nom à afficher, et récupère le fichier "
    "avec le filigrane retiré et le pied de page personnalisé."
)

uploaded_file = st.file_uploader("Fichier PDF", type=["pdf"])
new_name = st.text_input("Nom à afficher (ex : TOHOUINDJI E. Alice)")

if st.button("Générer", type="primary"):
    if uploaded_file is None:
        st.error("Merci d'ajouter un fichier PDF.")
    elif not new_name.strip():
        st.error("Merci de saisir un nom.")
    else:
        with st.spinner("Traitement en cours..."):
            try:
                pdf_bytes = uploaded_file.read()
                final_bytes, pages_without_marker = process_pdf(pdf_bytes, new_name.strip())
            except Exception as e:
                st.error(f"Une erreur est survenue : {e}")
            else:
                if pages_without_marker:
                    st.warning(
                        "Le filigrane/pied de page n'a pas été trouvé sur la ou les "
                        f"page(s) : {', '.join(map(str, pages_without_marker))}. "
                        "Ces pages ont été laissées telles quelles."
                    )
                st.success("Terminé !")
                base_name = uploaded_file.name.rsplit(".", 1)[0]
                out_filename = f"{base_name}_{new_name.strip().replace(' ', '_')}.pdf"
                st.download_button(
                    "⬇️ Télécharger le PDF final",
                    data=final_bytes,
                    file_name=out_filename,
                    mime="application/pdf",
                )
