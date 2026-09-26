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


def detect_fiches_from_pages(pages):
    """Retourne une liste de (start_idx, end_idx) 0-based : les bornes de
    pages de chaque fiche detectee, a partir d'une liste de pages
    pdfplumber DEJA OUVERTES (pour eviter de reparser le PDF plusieurs
    fois)."""
    starts = []
    n = len(pages)
    for i, page in enumerate(pages):
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


def extract_fiche_blocks_from_pages(pages, start_idx, end_idx):
    """Extrait, dans l'ordre de lecture, tous les blocs (paragraphes et
    lignes de tableau) des pages [start_idx, end_idx] d'une fiche, a
    partir d'une liste de pages pdfplumber DEJA OUVERTES."""
    all_blocks = []
    for i in range(start_idx, end_idx + 1):
        all_blocks.extend(extract_page_blocks(pages[i]))
    return all_blocks


# =============================================================================
# FILIGRANE DIAGONAL (modele actuel, applique a toutes les fiches)
# =============================================================================
#
# DIAGONAL_COVERAGE controle la largeur du filigrane : la proportion de la
# diagonale de la page que le nom doit couvrir. Reduit par rapport a
# l'ancienne valeur (0.92) pour un filigrane un peu moins large.

DIAGONAL_COVERAGE = 0.78
DIAGONAL_FONT_NAME = "Helvetica-Bold"


def draw_diagonal_watermark(canvas_obj, page_w, page_h, text):
    """Dessine un filigrane diagonal (coin bas-gauche -> coin haut-droit),
    centre, en une seule ligne (nom + mention "Realisee et concue par ..."
    deja combines par l'appelant dans `text`).

    Rendu en CONTOUR NOIR PUR (mode de rendu de texte "stroke", sans
    remplissage) : seul le bord des lettres est trace en noir, l'interieur
    reste transparent, exactement comme un tampon. Deux avantages par
    rapport a un remplissage plein ou a une transparence /ca :
      - un contour vectoriel reste net sur TOUS les lecteurs PDF (aucune
        transparence n'est utilisee, donc rien qui puisse etre mal gere
        par un lecteur PDF) ;
      - l'interieur transparent des lettres laisse voir le contenu/fond de
        la page a travers, meme si ce filigrane est place au premier plan."""
    if not text:
        return
    angle_rad = math.atan2(page_h, page_w)
    angle_deg = math.degrees(angle_rad)
    diag_len = math.hypot(page_w, page_h)
    target_span = DIAGONAL_COVERAGE * diag_len

    font_size = 40.0
    local_width = stringWidth(text, DIAGONAL_FONT_NAME, font_size)
    if local_width <= 0:
        local_width = font_size * max(len(text), 1) * 0.5
    scale = target_span / local_width
    final_font_size = font_size * scale
    final_width = local_width * scale
    line_width = max(0.5, final_font_size * 0.016)

    canvas_obj.saveState()
    canvas_obj.translate(page_w / 2, page_h / 2)
    canvas_obj.rotate(angle_deg)
    canvas_obj.setLineWidth(line_width)
    canvas_obj.setStrokeColor(colors.black)

    text_obj = canvas_obj.beginText(-final_width / 2, -final_font_size * 0.35)
    text_obj.setFont(DIAGONAL_FONT_NAME, final_font_size)
    text_obj.setTextRenderMode(1)  # 1 = contour seul (stroke), pas de remplissage
    text_obj.setStrokeColor(colors.black)
    text_obj.textOut(text)
    canvas_obj.drawText(text_obj)

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

    diagonal_text = f"Réalisée et conçue par {watermark_text}" if watermark_text else None

    # On compte les pages generees directement via les callbacks de
    # construction (au lieu de re-parser le PDF resultant avec PdfReader) :
    # ca evite de reanalyser tout le fichier juste pour connaitre son
    # nombre de pages, ce qui etait un des points lents de la recomposition.
    page_counter = {"n": 0}

    def _on_page(c, d):
        page_counter["n"] += 1
        draw_diagonal_watermark(c, page_size[0], page_size[1], diagonal_text)

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    buf.seek(0)
    data = buf.read()
    nb_pages = page_counter["n"]
    return data, nb_pages


def fit_fiche_to_two_pages(blocks, max_pages=MAX_PAGES_PER_FICHE, watermark_text=None):
    """Cherche la plus GRANDE taille de police (donc la plus lisible) qui
    fait tenir la fiche dans max_pages pages.

    Optimisation : plutot que de tester les tailles de police une par une
    en partant de la plus grande (jusqu'a 12 rendus complets dans le pire
    cas), on procede par recherche dichotomique sur la liste ordonnee des
    candidats. Le nombre de pages ne peut que diminuer (ou rester egal)
    quand on descend dans la liste (police/marges plus petites), donc
    quelques rendus (environ log2(12) ~= 4) suffisent au lieu de 12,
    pour un resultat strictement identique."""
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
    n = len(candidates)
    cache = {}

    def render(i):
        if i not in cache:
            font_size, leading_factor, margin = candidates[i]
            data, nb_pages = render_fiche_pages(
                blocks, font_size, leading_factor, margin, watermark_text=watermark_text
            )
            cache[i] = (data, nb_pages, font_size)
        return cache[i]

    lo, hi = 0, n - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        data, nb_pages, font_size = render(mid)
        if nb_pages <= max_pages:
            best = (data, nb_pages, font_size)
            hi = mid - 1
        else:
            lo = mid + 1

    if best is not None:
        return best
    # Aucun candidat ne suffit (fiche vraiment tres longue) : on garde le
    # plus petit essaye plutot que de produire une fiche vide.
    return render(n - 1)


def reformat_pdf(pdf_bytes, watermark_text=None, max_pages=MAX_PAGES_PER_FICHE):
    """Repere chaque fiche dans le PDF et recompose celles qui depassent
    max_pages pour qu'elles tiennent dans la limite, sans toucher aux
    fiches deja assez courtes. Retourne (pdf_bytes, rapport, pages_recomposees)
    ou rapport est une liste de dicts (une entree par fiche) et
    pages_recomposees est l'ensemble des indices 0-based, dans le PDF de
    sortie, des pages regenerees par reportlab (elles contiennent deja le
    filigrane correct, dessine directement, et ne doivent donc pas etre
    signalees comme "filigrane non trouve" lors de l'etape suivante).

    Le PDF source n'est ouvert avec pdfplumber qu'UNE SEULE FOIS (au lieu
    d'une fois pour reperer les fiches puis une fois par fiche trop
    longue) : c'etait le principal point lent sur les PDF a nombreuses
    fiches."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    report = []
    reflowed_pages = set()
    out_idx = 0

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = pdf.pages
        ranges = detect_fiches_from_pages(pages)

        for idx, (s, e) in enumerate(ranges):
            nb_orig = e - s + 1
            if nb_orig <= max_pages:
                for p in range(s, e + 1):
                    writer.add_page(reader.pages[p])
                    out_idx += 1
                report.append({"fiche": idx + 1, "pages_avant": nb_orig, "pages_apres": nb_orig, "modifiee": False})
                continue

            blocks = extract_fiche_blocks_from_pages(pages, s, e)
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
# /Footer, /Header ou /Watermark.
#
# Selon le logiciel/modele source, le contenu de ce bloc peut prendre
# plusieurs formes tres differentes :
#   - du texte ecrit directement (operateurs Tj / TJ) ;
#   - un appel a un objet forme externe (Form XObject, "/Fm1 Do") qui
#     contient lui-meme le texte, souvent en diagonal ;
#   - un appel a une IMAGE (Image XObject, "/Im0 Do") : c'est le cas des
#     nouveaux modeles de fiches, ou le filigrane est une image matricielle
#     (tampon repete avec nom/telephone/e-mail), et non plus du texte
#     modifiable.
#
# Essayer de reperer puis reecrire le texte a l'interieur de chacun de ces
# formats (comme le faisait la version precedente) est fragile : des qu'un
# nouveau modele de fiche apparait (ex. le filigrane-image), la detection
# echoue silencieusement et l'ancien filigrane reste en place.
#
# Approche plus robuste, utilisee ici : on ne cherche PAS a comprendre ou
# modifier le contenu du bloc. On se contente de reperer les blocs marques
# /Artifact /Pagination (quel que soit ce qu'ils contiennent : texte,
# Form XObject ou Image XObject) et on les SUPPRIME entierement. Le
# filigrane diagonal "maison" (draw_diagonal_watermark) est ensuite
# ajoute par-dessus, de facon uniforme, quel que soit le modele d'origine.

ARTIFACT_RE = re.compile(
    rb"/Artifact\s*<<(?P<dict>[^>]*)>>\s*BDC(?P<body>.*?)EMC",
    re.DOTALL,
)

# Types de pagination reconnus : pied de page, en-tete, filigrane
PAGINATION_SUBTYPES = (b"/Footer", b"/Header", b"/Watermark")


def is_pagination_artifact(dict_bytes: bytes) -> bool:
    return b"/Pagination" in dict_bytes and any(
        s in dict_bytes for s in PAGINATION_SUBTYPES
    )


def strip_pagination_artifacts(page: pikepdf.Page) -> bool:
    """Retire de la page tous les blocs marques /Artifact /Pagination
    (pied de page, en-tete, filigrane), quel que soit leur contenu
    (texte direct, Form XObject ou Image XObject). Retourne True si au
    moins un bloc a ete retire."""
    # Fusionne les flux de contenu multiples en un seul, sinon on ne peut pas
    # analyser/editer le flux de la page de facon fiable.
    page.contents_coalesce()
    contents = page.obj["/Contents"]
    data = contents.read_bytes()

    matches = list(ARTIFACT_RE.finditer(data))
    if not matches:
        return False

    removed = False
    new_data = data
    # On parcourt les correspondances en partant de la fin du flux pour ne
    # pas decaler les positions des correspondances precedentes lors des
    # suppressions de blocs.
    for m in reversed(matches):
        if is_pagination_artifact(m.group("dict")):
            new_data = new_data[: m.start()] + new_data[m.end():]
            removed = True

    if removed:
        contents.write(new_data)
    return removed


def personalize_artifacts(pdf_bytes: bytes):
    """Parcourt toutes les pages et retire tous les filigranes / pieds de
    page / en-tetes detectes, quel que soit leur format d'origine."""
    pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    pages_without_marker = []
    for i, page in enumerate(pdf.pages):
        found = strip_pagination_artifacts(page)
        if not found:
            pages_without_marker.append(i + 1)
    out = io.BytesIO()
    pdf.save(out)
    out.seek(0)
    return out.read(), pages_without_marker


def build_watermark_overlay(page_w: float, page_h: float, diagonal_text: str) -> bytes:
    """Construit un calque PDF (1 page) contenant uniquement le filigrane
    diagonal, aux dimensions reelles de la page."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w, page_h))
    draw_diagonal_watermark(c, page_w, page_h, diagonal_text)
    c.save()
    buf.seek(0)
    return buf.read()


def add_diagonal_watermark_overlay(pdf_bytes: bytes, new_name: str, skip_pages=None) -> bytes:
    """Ajoute le filigrane diagonal "maison" (nom + mention "Realisee et
    concue par ..." sur une seule ligne) sur toutes les pages, sauf celles
    listees dans `skip_pages` (indices 0-based) : ce sont les pages deja
    recomposees par la mise en forme automatique, qui contiennent deja ce
    filigrane, dessine directement lors de leur generation."""
    skip_pages = skip_pages or set()
    diagonal_text = f"Réalisée et conçue par {new_name}"
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()

    overlay_cache = {}
    for idx, page in enumerate(reader.pages):
        if idx in skip_pages:
            writer.add_page(page)
            continue

        left = float(page.mediabox.left)
        bottom = float(page.mediabox.bottom)
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)
        key = (round(w, 1), round(h, 1))
        if key not in overlay_cache:
            overlay_bytes = build_watermark_overlay(w, h, diagonal_text)
            overlay_cache[key] = PdfReader(io.BytesIO(overlay_bytes)).pages[0]

        # over=False : le filigrane est place SOUS le contenu reel de la
        # page (et non par-dessus). Le texte d'origine, dessine apres/
        # au-dessus, reste donc parfaitement net et lisible ; le contour du
        # filigrane apparait partout ailleurs (zones blanches de la page).
        if left or bottom:
            page.merge_transformed_page(
                overlay_cache[key], Transformation().translate(left, bottom), over=False
            )
        else:
            page.merge_page(overlay_cache[key], over=False)
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()


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


def process_pdf(pdf_bytes: bytes, new_name: str, watermark_mode: str, add_footer: bool, reformat: bool = False):
    """watermark_mode controle ce qui est fait du filigrane existant :
      - "replace" : le filigrane (quel que soit son format d'origine) est
        supprime puis remplace par le nouveau filigrane diagonal (nom du
        client + mention "Realisee et concue par ...").
      - "remove"  : le filigrane est supprime, sans qu'aucun nouveau
        filigrane ne soit ajoute. Le fichier ressort donc sans filigrane
        du tout (pied de page toujours optionnel via `add_footer`).
      - "keep"    : le filigrane d'origine n'est pas touche.
    Dans tous les cas, `add_footer` reste independant : il ajoute (ou pas)
    la bande "Realisee et concue par ..." en bas de page, quel que soit le
    traitement applique au filigrane."""
    reformat_report = None
    reflowed_pages = set()
    if reformat:
        pdf_bytes, reformat_report, reflowed_pages = reformat_pdf(
            pdf_bytes,
            # Les pages recomposees sont entierement regenerees a partir du
            # texte extrait : l'ancien filigrane (image ou texte) n'existe
            # plus sur ces pages une fois recomposees, quel que soit le mode
            # choisi. On ne dessine donc un nouveau filigrane dessus que si
            # on est en mode "replace".
            watermark_text=(new_name if watermark_mode == "replace" else None),
            max_pages=MAX_PAGES_PER_FICHE,
        )

    pages_without_marker = []
    if watermark_mode in ("replace", "remove"):
        stripped_bytes, pages_without_marker = personalize_artifacts(pdf_bytes)
        # Les pages recomposees par la mise en forme automatique contiennent
        # deja le filigrane correct (dessine directement par reportlab lors
        # de leur generation, mode "replace") ou n'en contiennent aucun
        # (modes "remove"/"keep") : dans les deux cas, il n'y a pas de sens
        # a les signaler comme "filigrane non trouve".
        pages_without_marker = [
            p for p in pages_without_marker if (p - 1) not in reflowed_pages
        ]
        if watermark_mode == "replace":
            final_bytes_wm = add_diagonal_watermark_overlay(stripped_bytes, new_name, skip_pages=reflowed_pages)
        else:
            final_bytes_wm = stripped_bytes
    else:  # "keep"
        final_bytes_wm = pdf_bytes

    final_bytes = add_footer_band(final_bytes_wm, new_name) if add_footer else final_bytes_wm
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
        <p>Detection automatique du filigrane existant : remplacement, suppression pure, ou pied de page (nom du client) sur un ou plusieurs PDF.</p>
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
st.markdown("**Filigrane existant**")
_WATERMARK_MODE_LABELS = {
    "replace": "Le remplacer par un nouveau filigrane (nom du client)",
    "remove": "Le supprimer, sans en ajouter un nouveau",
    "keep": "Ne pas y toucher",
}
watermark_mode_label = st.radio(
    "Que faire du filigrane existant ?",
    list(_WATERMARK_MODE_LABELS.values()),
    index=0,
    label_visibility="collapsed",
)
watermark_mode = next(
    k for k, v in _WATERMARK_MODE_LABELS.items() if v == watermark_mode_label
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
    elif watermark_mode == "keep" and not add_footer:
        st.error("Merci de sélectionner au moins une action (filigrane et/ou pied de page).")
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
                    pdf_bytes, new_name.strip(), watermark_mode, add_footer, reformat
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
        st.session_state["results_watermark_mode"] = watermark_mode

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
            _mode = st.session_state.get("results_watermark_mode", "replace")
            if _mode == "keep":
                st.caption("Filigrane non modifie (option « Ne pas y toucher »).")
            elif _mode == "remove":
                if pages_without_marker:
                    st.caption(
                        "ℹ️ Aucun filigrane detecte sur la ou les page(s) : "
                        f"{', '.join(map(str, pages_without_marker))} (rien a supprimer)."
                    )
                else:
                    st.caption("🧹 Filigrane supprime sur toutes les pages (aucun nouveau filigrane ajoute).")
            else:  # "replace"
                if pages_without_marker:
                    st.caption(
                        "ℹ️ Aucun ancien filigrane detecte sur la ou les page(s) : "
                        f"{', '.join(map(str, pages_without_marker))} (rien a supprimer). "
                        "Le nouveau filigrane a tout de meme ete ajoute par-dessus sur ces pages."
                    )
                else:
                    st.caption("Ancien filigrane detecte, supprime et remplace par le nouveau sur toutes les pages.")

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
