import io
import re
import math
import zipfile
import streamlit as st
import pikepdf
import pdfplumber
from pypdf import PdfReader, PdfWriter, Transformation
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.lib.colors import white, black
from reportlab.pdfbase.pdfmetrics import stringWidth

# =============================================================================
# MISE EN FORME AUTOMATIQUE (max 2 pages par fiche)
# =============================================================================
#
# Objectif : lire un PDF contenant plusieurs fiches de lecons a la suite,
# reperer ou commence et ou finit chaque fiche, et recomposer celles qui
# depassent 2 pages pour qu'elles tiennent en 2 pages maximum, SANS changer
# un seul mot du contenu original (seules la police/les marges changent).
#
# Detection des fiches : le marqueur "Cours : CI/CP/CE1/CE2/CM1/CM2" apparait
# toujours au debut de chaque fiche, quel que soit le champ de formation.
# C'est ce marqueur qui sert a decouper le PDF fiche par fiche.

COURS_RE = re.compile(r"Cours\s*:\s*(CI|CP|CE1|CE2|CM1|CM2)\b")
MAX_PAGES_PER_FICHE = 2


def detect_fiches(pdf_path_or_bytes):
    """Retourne une liste de (start_idx, end_idx) 0-based : les bornes de
    pages de chaque fiche detectee dans le PDF."""
    starts = []
    with pdfplumber.open(pdf_path_or_bytes) as pdf:
        n = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if COURS_RE.search(text):
                starts.append(i)
    if not starts:
        return [(0, n - 1)] if n else []
    if starts[0] != 0:
        starts = [0] + starts
    ranges = []
    for idx, s in enumerate(starts):
        e = (starts[idx + 1] - 1) if idx + 1 < len(starts) else n - 1
        ranges.append((s, e))
    return ranges


def clean_join(text):
    """Recolle un texte multi-lignes (cellule de tableau) en une seule
    ligne, toujours avec une espace. On ne tente jamais de deviner une
    coupure de mot (risque de fusionner par erreur deux mots distincts) :
    au pire, un mot coupe par la mise en page d'origine garde un espace au
    milieu, exactement comme il apparaissait deja coupe visuellement dans
    le document source."""
    if not text:
        return ""
    parts = [p.strip() for p in text.split("\n")]
    return " ".join(p for p in parts if p).strip()


def extract_page_blocks(page):
    """Retourne les blocs de contenu d'une page, ordonnes verticalement :
    ('row', top, label, description) pour chaque ligne de tableau, ou
    ('para', top, texte) pour le texte hors tableau."""
    tables = page.find_tables()
    blocks = []

    for t in tables:
        for row in t.rows:
            cells_bbox = row.cells
            valid_boxes = [c for c in cells_bbox if c]
            if not valid_boxes:
                continue
            row_top = min(c[1] for c in valid_boxes)
            cropped_cells = []
            for c in cells_bbox:
                if c is None:
                    cropped_cells.append("")
                    continue
                x0, top, x1, bottom = c
                crop = page.within_bbox((x0, top, x1, bottom))
                cropped_cells.append(clean_join(crop.extract_text() or ""))
            if len(cropped_cells) >= 2:
                if any(c.strip() for c in cropped_cells):
                    blocks.append(("row", row_top, cropped_cells[0], " ".join(cropped_cells[1:])))
            elif cropped_cells and cropped_cells[0].strip():
                blocks.append(("para", row_top, cropped_cells[0]))

    def in_any_table(word):
        for t in tables:
            x0, top, x1, bottom = t.bbox
            if word["x0"] >= x0 - 1 and word["x1"] <= x1 + 1 and word["top"] >= top - 1 and word["bottom"] <= bottom + 1:
                return True
        return False

    words = [w for w in page.extract_words() if not in_any_table(w)]
    words.sort(key=lambda w: w["top"])
    lines = []
    for w in words:
        placed = False
        for line in lines:
            if abs(line["top"] - w["top"]) <= 3:
                line["words"].append(w)
                line["top"] = min(line["top"], w["top"])
                placed = True
                break
        if not placed:
            lines.append({"top": w["top"], "words": [w]})
    for line in lines:
        ws = sorted(line["words"], key=lambda w: w["x0"])
        line_text = " ".join(w["text"] for w in ws)
        blocks.append(("para", line["top"], line_text))

    blocks.sort(key=lambda b: b[1])
    return blocks


def extract_fiche_blocks(pdf_path_or_bytes, start_idx, end_idx):
    """Extrait, dans l'ordre de lecture, tous les blocs (paragraphes et
    lignes de tableau) des pages [start_idx, end_idx] d'une fiche."""
    all_blocks = []
    with pdfplumber.open(pdf_path_or_bytes) as pdf:
        for i in range(start_idx, end_idx + 1):
            all_blocks.extend(extract_page_blocks(pdf.pages[i]))
    return all_blocks


def draw_diagonal_watermark(canvas_obj, page_w, page_h, text):
    """Dessine un filigrane diagonal (coin bas-gauche -> coin haut-droit),
    centre, semi-transparent, quel que soit le texte."""
    if not text:
        return
    angle_rad = math.atan2(page_h, page_w)
    angle_deg = math.degrees(angle_rad)
    diag_len = math.hypot(page_w, page_h)
    target_span = 0.92 * diag_len

    font_size = 40.0
    local_width = stringWidth(text, "Helvetica-Bold", font_size)
    if local_width <= 0:
        local_width = font_size * max(len(text), 1) * 0.5
    scale = target_span / local_width
    final_font_size = font_size * scale

    canvas_obj.saveState()
    canvas_obj.setFillColor(colors.black)
    try:
        canvas_obj.setFillAlpha(0.08)
    except Exception:
        pass
    canvas_obj.translate(page_w / 2, page_h / 2)
    canvas_obj.rotate(angle_deg)
    canvas_obj.setFont("Helvetica-Bold", final_font_size)
    canvas_obj.drawCentredString(0, -final_font_size * 0.35, text)
    canvas_obj.restoreState()


def render_fiche_pages(blocks, font_size=9.5, leading_factor=1.18,
                        margin=1.5 * cm, page_size=A4, watermark_text=None):
    """Recompose les blocs en 1 ou 2 pages via reportlab. Retourne
    (pdf_bytes, nb_pages)."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=page_size,
        leftMargin=margin, rightMargin=margin,
        topMargin=margin, bottomMargin=margin,
    )
    para_style = ParagraphStyle(
        "fiche_para", fontName="Helvetica", fontSize=font_size,
        leading=font_size * leading_factor, spaceAfter=1.2,
    )
    label_style = ParagraphStyle(
        "fiche_label", fontName="Helvetica-Bold", fontSize=font_size,
        leading=font_size * leading_factor,
    )
    desc_style = ParagraphStyle(
        "fiche_desc", fontName="Helvetica", fontSize=font_size,
        leading=font_size * leading_factor,
    )

    def esc(t):
        return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    story = []
    avail_width = page_size[0] - 2 * margin
    col1 = avail_width * 0.22
    col2 = avail_width - col1

    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b[0] == "row":
            rows_data = []
            while i < len(blocks) and blocks[i][0] == "row":
                _, _, label, desc = blocks[i]
                rows_data.append([
                    Paragraph(esc(label), label_style),
                    Paragraph(esc(desc), desc_style),
                ])
                i += 1
            tbl = Table(rows_data, colWidths=[col1, col2])
            tbl.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]))
            story.append(tbl)
            story.append(Spacer(1, font_size * 0.3))
        else:
            text = b[2].strip()
            if text:
                story.append(Paragraph(esc(text), para_style))
            else:
                story.append(Spacer(1, font_size * 0.3))
            i += 1

    _draw_wm = lambda c, d: draw_diagonal_watermark(c, page_size[0], page_size[1], watermark_text)
    doc.build(story, onFirstPage=_draw_wm, onLaterPages=_draw_wm)
    buf.seek(0)
    data = buf.read()
    nb_pages = len(PdfReader(io.BytesIO(data)).pages)
    return data, nb_pages


def fit_fiche_to_two_pages(blocks, max_pages=MAX_PAGES_PER_FICHE, watermark_text=None):
    """Cherche la plus GRANDE taille de police (donc la plus lisible) qui
    fait tenir la fiche dans max_pages pages, en descendant progressivement
    si necessaire (jamais en dessous d'un seuil de lisibilite)."""
    candidates = [
        (12.5, 1.22, 2.0 * cm),
        (12.0, 1.20, 1.9 * cm),
        (11.5, 1.20, 1.8 * cm),
        (11.0, 1.19, 1.7 * cm),
        (10.5, 1.18, 1.6 * cm),
        (10.0, 1.18, 1.6 * cm),
        (9.5, 1.18, 1.5 * cm),
        (9.0, 1.15, 1.3 * cm),
        (8.5, 1.13, 1.2 * cm),
        (8.0, 1.10, 1.1 * cm),
        (7.5, 1.08, 1.0 * cm),
        (7.0, 1.05, 0.9 * cm),
    ]
    last = None
    for font_size, leading_factor, margin in candidates:
        data, nb_pages = render_fiche_pages(blocks, font_size, leading_factor, margin, watermark_text=watermark_text)
        last = (data, nb_pages, font_size)
        if nb_pages <= max_pages:
            return data, nb_pages, font_size
    return last


def reformat_pdf(pdf_bytes, watermark_text=None, max_pages=MAX_PAGES_PER_FICHE):
    """Repere chaque fiche dans le PDF et recompose celles qui depassent
    max_pages pour qu'elles tiennent dans la limite, sans toucher aux
    fiches deja assez courtes. Retourne (pdf_bytes, rapport, pages_recomposees)
    ou rapport est une liste de dicts (une entree par fiche) et
    pages_recomposees est l'ensemble des indices 0-based, dans le PDF de
    sortie, des pages regenerees par reportlab (elles contiennent deja le
    filigrane correct, dessine directement, et ne doivent donc pas etre
    signalees comme "filigrane non trouve" lors de l'etape suivante)."""
    buf = io.BytesIO(pdf_bytes)
    ranges = detect_fiches(buf)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    report = []
    reflowed_pages = set()
    out_idx = 0

    for idx, (s, e) in enumerate(ranges):
        nb_orig = e - s + 1
        if nb_orig <= max_pages:
            for p in range(s, e + 1):
                writer.add_page(reader.pages[p])
                out_idx += 1
            report.append({"fiche": idx + 1, "pages_avant": nb_orig, "pages_apres": nb_orig, "modifiee": False})
            continue

        blocks = extract_fiche_blocks(io.BytesIO(pdf_bytes), s, e)
        if not blocks:
            # Echec d'extraction : on garde les pages originales plutot que
            # de produire une fiche vide.
            for p in range(s, e + 1):
                writer.add_page(reader.pages[p])
                out_idx += 1
            report.append({"fiche": idx + 1, "pages_avant": nb_orig, "pages_apres": nb_orig, "modifiee": False})
            continue

        data, nb_pages, font_size = fit_fiche_to_two_pages(blocks, max_pages, watermark_text)
        sub_reader = PdfReader(io.BytesIO(data))
        for p in sub_reader.pages:
            writer.add_page(p)
            reflowed_pages.add(out_idx)
            out_idx += 1
        report.append({
            "fiche": idx + 1, "pages_avant": nb_orig, "pages_apres": nb_pages,
            "modifiee": True, "police": font_size,
        })

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read(), report, reflowed_pages



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


CM_MATRIX_RE = re.compile(
    rb"(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+cm"
)
FONT_SIZE_RE = re.compile(rb"/\S+\s+(-?[\d.]+)\s+Tf")
TD_OFFSET_RE = re.compile(rb"(-?[\d.]+)\s+(-?[\d.]+)\s+Td")

# Fraction de la longueur de la diagonale de la page que le texte du filigrane
# doit couvrir (marge de securite pour ne pas toucher les bords).
DIAGONAL_COVERAGE = 0.92


def fmt_num(x: float) -> bytes:
    return f"{x:.6f}".encode("ascii")


def compute_diagonal_cm(page_w: float, page_h: float, text: str, font_size: float,
                         baseline_x: float = 0.0, baseline_y: float = 0.0):
    """Calcule une matrice 'cm' (rotation + echelle uniforme) qui etire le
    filigrane le long de la vraie diagonale de la page (coin bas-gauche ->
    coin haut-droit), centree sur la page, quel que soit le nom.
    baseline_x/baseline_y : position locale (avant cm) ou le texte est
    reellement dessine (issue du Td d'origine conserve par rewrite_text_in_stream),
    necessaire pour bien calculer le centre reel du bloc de texte."""
    angle = math.atan2(page_h, page_w)
    diag_len = math.hypot(page_w, page_h)
    target_span = DIAGONAL_COVERAGE * diag_len

    # Largeur du texte, approximee avec les metriques Helvetica (tres
    # proches d'Arial, police generalement utilisee dans ces gabarits).
    local_width = stringWidth(text, "Helvetica", font_size)
    if local_width <= 0:
        local_width = font_size * max(len(text), 1) * 0.5

    scale = target_span / local_width

    a = scale * math.cos(angle)
    b = scale * math.sin(angle)
    c = -scale * math.sin(angle)
    d = scale * math.cos(angle)

    # Centre local reel du texte : on part de la position de depart (Td
    # d'origine), on ajoute la moitie de la largeur en x, et environ 35% de
    # la taille de police au-dessus de la ligne de base en y.
    cx = baseline_x + local_width / 2
    cy = baseline_y + font_size * 0.35

    page_cx = page_w / 2
    page_cy = page_h / 2
    e = page_cx - (a * cx + c * cy)
    f = page_cy - (b * cx + d * cy)

    return a, b, c, d, e, f


def resize_diagonal_watermark(page_content: bytes, body_start: int, body_end: int,
                               page_w: float, page_h: float, new_text: str,
                               font_size: float, baseline_x: float, baseline_y: float) -> bytes:
    """Remplace la matrice 'cm' precedant l'appel '/Xxx Do' du filigrane par
    une matrice recalculee pour que le texte s'etire d'un coin a l'autre de
    la diagonale de la page, quelle que soit la longueur du nom."""
    body = page_content[body_start:body_end]
    m = CM_MATRIX_RE.search(body)
    if not m:
        return page_content
    a, b, c, d, e, f = compute_diagonal_cm(
        page_w, page_h, new_text, font_size, baseline_x, baseline_y
    )
    new_cm = b" ".join(fmt_num(v) for v in (a, b, c, d, e, f)) + b" cm"
    new_body = body[: m.start()] + new_cm + body[m.end():]
    return page_content[:body_start] + new_body + page_content[body_end:]


def process_page_artifacts(page: pikepdf.Page, new_text: str):
    """Traite tous les artefacts de pagination d'une page : remplace le texte
    (direct ou via XObject). Retourne True si au moins un artefact a ete traite."""
    # Fusionne les flux de contenu multiples en un seul, sinon on ne peut pas
    # analyser/editer le flux de la page de facon fiable.
    page.contents_coalesce()
    contents = page.obj["/Contents"]
    data = contents.read_bytes()

    mbox = page.mediabox
    page_w = float(mbox[2]) - float(mbox[0])
    page_h = float(mbox[3]) - float(mbox[1])

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
        # en gardant sa police d'origine, puis on recalcule la matrice de
        # positionnement/rotation dans le flux de la page pour que le
        # filigrane s'etire toujours d'un coin a l'autre de la diagonale,
        # quelle que soit la longueur du nouveau nom.
        do_match = XOBJECT_DO_RE.search(body)
        if do_match and xobjects is not None:
            xobj_name = "/" + do_match.group("name").decode("latin-1")
            if xobj_name in xobjects:
                xobj = xobjects[xobj_name]
                xdata = xobj.read_bytes()
                fsize_match = FONT_SIZE_RE.search(xdata)
                font_size = float(fsize_match.group(1)) if fsize_match else 24.0
                td_match = TD_OFFSET_RE.search(xdata)
                baseline_x = float(td_match.group(1)) if td_match else 0.0
                baseline_y = float(td_match.group(2)) if td_match else 0.0
                xnew, ok = rewrite_text_in_stream(xdata, new_text)
                if ok:
                    xobj.write(xnew)
                    # Le Form XObject recadre (clip) tout ce qui est dessine
                    # en dehors de son /BBox d'origine : celui-ci etait
                    # calibre pour l'ancien nom (souvent plus court/long que
                    # le nouveau), donc on l'agrandit pour couvrir le nouveau
                    # texte en entier, quelle que soit sa longueur.
                    local_w = stringWidth(new_text, "Helvetica", font_size)
                    pad = font_size * 0.3
                    xobj["/BBox"] = pikepdf.Array([
                        min(0.0, baseline_x) - pad,
                        baseline_y - pad,
                        baseline_x + local_w + pad,
                        baseline_y + font_size + pad,
                    ])
                    handled = True
                    body_start, body_end = m.start("body"), m.end("body")
                    new_data = resize_diagonal_watermark(
                        new_data, body_start, body_end, page_w, page_h,
                        new_text, font_size, baseline_x, baseline_y,
                    )

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
    positionne proportionnellement a la taille reelle de la page. La bande
    couvre presque toute la largeur (pour effacer tout residu existant) et
    la taille du texte s'adapte pour toujours tenir et rester bien centree,
    quelle que soit la longueur du nom."""
    scale_y = page_h / REF_PAGE_H

    margin = 12
    rect_x0 = margin
    rect_x1 = page_w - margin
    rect_y0 = page_h - (FOOTER_BOTTOM_FROM_TOP * scale_y) - 3
    rect_y1 = page_h - (FOOTER_TOP_FROM_TOP * scale_y) + 3

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.setFillColor(white)
    c.rect(rect_x0, rect_y0, rect_x1 - rect_x0, rect_y1 - rect_y0, fill=1, stroke=0)

    # La taille de police s'adapte a la longueur du nom pour toujours tenir
    # dans la bande disponible (avec un peu de marge interne) et rester
    # parfaitement centree, quel que soit le nom saisi.
    available_width = (rect_x1 - rect_x0) - 20
    font_size = 11.0
    min_font_size = 6.0
    tw = stringWidth(new_text, "Helvetica", font_size)
    if tw > available_width and available_width > 0:
        font_size = max(min_font_size, font_size * available_width / tw)
        tw = stringWidth(new_text, "Helvetica", font_size)

    c.setFillColor(black)
    c.setFont("Helvetica", font_size)
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
        left = float(page.mediabox.left)
        bottom = float(page.mediabox.bottom)
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)
        key = (round(w, 1), round(h, 1))
        if key not in overlay_cache:
            overlay_bytes = build_overlay(w, h, new_text)
            overlay_cache[key] = PdfReader(io.BytesIO(overlay_bytes)).pages[0]
        # On translate le calque a l'origine reelle de la page (certains PDF
        # ont une MediaBox qui ne commence pas a (0,0)), pour garantir que le
        # centrage du texte corresponde bien au centre visuel de la page.
        if left or bottom:
            page.merge_transformed_page(
                overlay_cache[key], Transformation().translate(left, bottom)
            )
        else:
            page.merge_page(overlay_cache[key])
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()


def process_pdf(pdf_bytes: bytes, new_name: str, add_watermark: bool, add_footer: bool, reformat: bool = False):
    reformat_report = None
    reflowed_pages = set()
    if reformat:
        pdf_bytes, reformat_report, reflowed_pages = reformat_pdf(
            pdf_bytes,
            watermark_text=(new_name if add_watermark else None),
            max_pages=MAX_PAGES_PER_FICHE,
        )
    if add_watermark:
        personalized_bytes, pages_without_marker = personalize_artifacts(pdf_bytes, new_name)
        # Les pages recomposees par la mise en forme automatique contiennent
        # deja le filigrane correct (dessine directement par reportlab, pas
        # via un bloc /Artifact) : on ne les signale pas comme "filigrane
        # non trouve", ce serait un faux avertissement.
        pages_without_marker = [
            p for p in pages_without_marker if (p - 1) not in reflowed_pages
        ]
    else:
        personalized_bytes, pages_without_marker = pdf_bytes, []
    final_bytes = add_footer_band(personalized_bytes, new_name) if add_footer else personalized_bytes
    return final_bytes, pages_without_marker, reformat_report


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
add_watermark = st.checkbox(
    "Remplacer le filigrane / nom existant sur chaque page",
    value=True,
)
add_footer = st.checkbox(
    "Ajouter aussi une bande « Réalisée et conçue par ... » en bas de chaque page",
    value=True,
)
reformat = st.checkbox(
    "Recomposer automatiquement les fiches de plus de 2 pages "
    "(police réduite pour tenir en 2 pages max, sans changer le texte)",
    value=False,
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
    elif not add_watermark and not add_footer:
        st.error("Merci de sélectionner au moins une option (filigrane et/ou pied de page).")
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
                final_bytes, pages_without_marker, reformat_report = process_pdf(
                    pdf_bytes, new_name.strip(), add_watermark, add_footer, reformat
                )
            except Exception as e:
                results.append((uploaded_file.name, None, None, None, str(e)))
            else:
                base_name = uploaded_file.name.rsplit(".", 1)[0]
                out_filename = f"{base_name}_{new_name.strip().replace(' ', '_')}.pdf"
                results.append((out_filename, final_bytes, pages_without_marker, reformat_report, None))
        progress.progress(1.0, text="Termine !")
        progress.empty()

        # On memorise les resultats dans la session : sans ca, cliquer sur un
        # bouton de telechargement declenche un rerun de la page et tout
        # redeviendrait vide (il faudrait tout regenerer).
        st.session_state["results"] = results
        st.session_state["results_name"] = new_name.strip()
        st.session_state["results_add_watermark"] = add_watermark

results = st.session_state.get("results")

if results:
    n_ok = sum(1 for r in results if r[4] is None)
    n_err = sum(1 for r in results if r[4] is not None)
    if n_err:
        st.warning(f"{n_ok} fichier(s) traite(s), {n_err} en erreur.")
    else:
        st.success(f"{n_ok} fichier(s) traite(s) avec succes !")

    # Telechargement groupe (.zip) si plusieurs fichiers ont reussi
    ok_files = [(name, data) for name, data, _, _, err in results if err is None]
    if len(ok_files) > 1:
        zip_bytes = make_zip(ok_files)
        st.download_button(
            "⬇️ Telecharger tous les fichiers (.zip)",
            data=zip_bytes,
            file_name=f"fiches_{st.session_state['results_name'].replace(' ', '_')}.zip",
            mime="application/zip",
            key="dl_zip",
        )

    st.markdown("### 3. Résultats")

    if st.button("🔄 Nouveau lot (effacer ces résultats)"):
        del st.session_state["results"]
        st.rerun()

    # Detail + telechargement individuel pour chaque fichier
    for out_filename, final_bytes, pages_without_marker, reformat_report, error in results:
        if error is not None:
            st.error(f"❌ {out_filename} : {error}")
            continue

        with st.container(border=True):
            st.write(f"**{out_filename}**")
            if not st.session_state.get("results_add_watermark", True):
                st.caption("Filigrane non modifie (option desactivee).")
            elif pages_without_marker:
                st.warning(
                    "Aucun filigrane reconnu sur la ou les page(s) : "
                    f"{', '.join(map(str, pages_without_marker))}. "
                    "Ces pages ont ete laissees telles quelles (seule la "
                    "bande de pied de page, si activee, a ete ajoutee)."
                )
            else:
                st.caption("Filigrane detecte et remplace sur toutes les pages.")

            if reformat_report:
                modifiees = [r for r in reformat_report if r["modifiee"]]
                if modifiees:
                    details = " · ".join(
                        f"fiche {r['fiche']} ({r['pages_avant']}→{r['pages_apres']} pages, "
                        f"police {r['police']:.1f}pt)"
                        for r in modifiees
                    )
                    st.caption(f"🗜️ Recomposees : {details}")
                else:
                    st.caption("🗜️ Aucune fiche ne depassait 2 pages : rien a recomposer.")

            st.download_button(
                "⬇️ Telecharger ce PDF",
                data=final_bytes,
                file_name=out_filename,
                mime="application/pdf",
                key=f"dl_{out_filename}",
            )
