# ============================================================
# PAPEDA — Scraper & Monitoring Berita Fenomena PDRB
# Cepat (paralel + cache + retry), tanpa Gemini & tanpa ringkasan AI.
# Fitur: multi-wilayah/usaha, deteksi liputan serupa (fuzzy), dashboard
#        analisis interaktif, riwayat pencarian, ekspor Excel/CSV.
# ============================================================

import streamlit as st
import time
import random
import re
import logging
import itertools
from typing import List, Dict, Tuple, Any, Optional
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email.utils import parsedate_to_datetime
import datetime as dt
import base64
import os
import numpy as np
import pandas as pd
import plotly.express as px
from rapidfuzz import fuzz, process
from tenacity import retry, stop_after_attempt, wait_random_exponential
from pygooglenews import GoogleNews
from googlenewsdecoder import gnewsdecoder
from st_aggrid import AgGrid, GridOptionsBuilder, JsCode
from io import BytesIO

# ============================================================
# 1. KONFIGURASI & VARIABEL GLOBAL
# ============================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [PAPEDA] %(levelname)s: %(message)s")
logger = logging.getLogger("papeda")

def _secret(key: str, default: str) -> str:
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default

SHEET_ID = _secret("papeda_sheet_id", "1cSISqNtyiGiyZ4nqTrTxBWIO7U98RBS5Z9ehBMWadYo")
SHEET_USAHA_GID = _secret("papeda_usaha_gid", "233383135")
SHEET_WILAYAH_GID = _secret("papeda_wilayah_gid", "0")

gn = GoogleNews(lang="id", country="ID")
DATE_DELTA = dt.timedelta(days=30)

MAX_RETRIES = 3
BASE_BACKOFF = 0.6  # detik, dasar exponential backoff + jitter
DUP_THRESHOLD = 82  # skor kemiripan judul (0-100, token_set_ratio) untuk dianggap liputan yang sama
DUP_MAX_ROWS = 4000  # guard rail: di atas ini, fallback ke pencocokan judul persis (hindari O(n^2) berat)

RESULT_COLUMNS = ["Tanggal", "Judul", "Sumber", "Wilayah", "Usaha", "Liputan Serupa", "Status URL", "URL"]
MAX_HISTORY = 8

STOPWORDS_ID = {
    "yang", "dan", "di", "ke", "dari", "untuk", "pada", "dengan", "ini", "itu", "akan", "adalah",
    "atau", "juga", "dalam", "tidak", "ada", "oleh", "secara", "telah", "sudah", "bisa", "dapat",
    "para", "per", "sebagai", "saat", "kata", "tahun", "persen", "yaitu", "sebuah", "seorang",
    "hingga", "serta", "namun", "tetapi", "karena", "sehingga", "antara", "lebih", "kurang",
    "masih", "belum", "dua", "tiga", "satu", "empat", "lima", "kami", "kita", "mereka", "dia",
    "nya", "pun", "lah", "kah", "apa", "siapa", "bagaimana", "mengapa", "kapan", "dimana",
    "atas", "bawah", "menjadi", "jadi", "terkait", "usai", "resmi", "the", "and", "for", "of",
    "in", "to", "is", "on", "with", "as", "at", "by",
}

# ============================================================
# 2. RETRY (tenacity) — Google News & decoder sering membalas
#    429/timeout saat diserbu paralel; retry membuat scraping stabil
#    dan mencegah kegagalan sesaat ikut ter-cache 1 jam.
# ============================================================

_retry_default = dict(stop=stop_after_attempt(MAX_RETRIES), wait=wait_random_exponential(multiplier=BASE_BACKOFF, max=6), reraise=True)

@retry(**_retry_default)
def _gn_search(keyword: str, from_: str, to_: str):
    return gn.search(keyword, from_=from_, to_=to_)

@retry(stop=stop_after_attempt(2), wait=wait_random_exponential(multiplier=BASE_BACKOFF, max=4), reraise=True)
def _decode(link: str):
    return gnewsdecoder(link)

@retry(**_retry_default)
def _read_csv(url: str) -> pd.DataFrame:
    return pd.read_csv(url)

# ============================================================
# 3. HELPER UMUM
# ============================================================

def empty_result_df() -> pd.DataFrame:
    return pd.DataFrame(columns=RESULT_COLUMNS)

def to_excel(df: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.drop(columns=["TanggalSort"], errors="ignore").to_excel(writer, index=False, sheet_name="Data")
    return output.getvalue()

def to_csv(df: pd.DataFrame) -> bytes:
    return df.drop(columns=["TanggalSort"], errors="ignore").to_csv(index=False).encode("utf-8-sig")

def explode_counts(series: pd.Series, top: int = 10) -> pd.Series:
    exploded = series.dropna().astype(str).str.split(", ").explode()
    exploded = exploded[exploded.str.strip() != ""]
    return exploded.value_counts().head(top).sort_values(ascending=True)

def cluster_similar_titles(titles: List[str], threshold: int = DUP_THRESHOLD) -> List[int]:
    """Kelompokkan judul yang MIRIP (bukan cuma identik) antar portal memakai
    rapidfuzz — menangkap berita hasil wire/syndication yang judulnya sedikit
    diubah tiap media. Dataset sangat besar fallback ke pencocokan judul persis
    supaya tidak membebani (matriks kemiripan tumbuh O(n^2))."""
    n = len(titles)
    if n == 0:
        return []

    if n > DUP_MAX_ROWS:
        seen: Dict[str, int] = {}
        ids = []
        for t in titles:
            key = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", t.lower())).strip()
            ids.append(seen.setdefault(key, len(seen)))
        return ids

    # token_set_ratio dipilih (bukan token_sort_ratio) karena tahan terhadap kata
    # tambahan/berbeda urutan antar media (mis. tahun/angka disisipkan, "kata BPS"
    # di akhir judul) — lebih cocok untuk gaya penulisan ulang berita wire di Indonesia.
    scores = process.cdist(titles, titles, scorer=fuzz.token_set_ratio, dtype=np.uint8)
    cluster_id = [-1] * n
    next_id = 0
    for i in range(n):
        if cluster_id[i] != -1:
            continue
        cluster_id[i] = next_id
        for j in np.where(scores[i] >= threshold)[0]:
            if cluster_id[j] == -1:
                cluster_id[j] = next_id
        next_id += 1
    return cluster_id

def extract_keywords(titles: List[str], exclude: set, top: int = 15) -> pd.Series:
    counter: Counter = Counter()
    for t in titles:
        for w in re.findall(r"[a-zA-Z]{4,}", t.lower()):
            if w in STOPWORDS_ID or w in exclude:
                continue
            counter[w] += 1
    if not counter:
        return pd.Series(dtype=int)
    return pd.Series(dict(counter.most_common(top))).sort_values(ascending=True)

# ============================================================
# 4. GRAFIK (Plotly, konsisten dengan tema aplikasi)
# ============================================================

_PLOTLY_LAYOUT = dict(
    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
    font=dict(family="Inter, sans-serif", color="#1E293B", size=12.5),
    margin=dict(l=10, r=16, t=10, b=10), showlegend=False,
)

def chart_bar_h(series: pd.Series, color: str):
    if series.empty:
        st.caption("Belum ada data yang cukup untuk grafik ini.")
        return
    data = series.reset_index()
    data.columns = ["Label", "Jumlah"]
    fig = px.bar(data, x="Jumlah", y="Label", orientation="h", text="Jumlah")
    fig.update_traces(marker_color=color, marker_line_width=0, textposition="outside", cliponaxis=False)
    fig.update_layout(**_PLOTLY_LAYOUT, xaxis=dict(showgrid=True, gridcolor="#E3E8EF"), yaxis=dict(title=None, showgrid=False))
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

def chart_area(series: pd.Series, color: str):
    if series.empty:
        st.caption("Tanggal artikel tidak dapat diproses untuk grafik ini.")
        return
    data = series.reset_index()
    data.columns = ["Tanggal", "Jumlah"]
    fig = px.area(data, x="Tanggal", y="Jumlah")
    fig.update_traces(line_color=color, fillcolor=color + "33")
    fig.update_layout(**_PLOTLY_LAYOUT, xaxis=dict(showgrid=False), yaxis=dict(showgrid=True, gridcolor="#E3E8EF", title=None))
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

# ============================================================
# 5. TABEL HASIL (AgGrid + tautan & badge status)
# ============================================================

_LINK_RENDERER = JsCode("""
class UrlCellRenderer {
    init(params) {
        this.eGui = document.createElement('a');
        this.eGui.innerText = params.value ? '🔗 Buka Artikel' : '-';
        this.eGui.setAttribute('href', params.value || '#');
        this.eGui.setAttribute('target', '_blank');
        this.eGui.setAttribute('rel', 'noopener noreferrer');
        this.eGui.style.color = '#1565C0';
        this.eGui.style.fontWeight = '600';
        this.eGui.style.textDecoration = 'none';
    }
    getGui() { return this.eGui; }
}
""")

_STATUS_STYLE = JsCode("""
function(params) {
    if (params.value === 'Asli') { return {color: '#15803D', fontWeight: '600'}; }
    return {color: '#B45309', fontWeight: '600'};
}
""")

_DUP_STYLE = JsCode("""
function(params) {
    if (params.value && params.value > 0) { return {color: '#7C3AED', fontWeight: '700'}; }
    return {color: '#94A3B8'};
}
""")

def show_aggrid(df: pd.DataFrame):
    df = df.drop(columns=["TanggalSort", "index"], errors="ignore").reset_index(drop=True)

    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_pagination(paginationPageSize=10)
    gb.configure_side_bar()
    gb.configure_default_column(editable=False, groupable=True, resizable=True)
    gb.configure_grid_options(enableRangeSelection=True, enableCellTextSelection=True, domLayout="normal")
    gb.configure_selection("multiple", use_checkbox=False)

    if "Judul" in df.columns:
        gb.configure_column("Judul", minWidth=340, wrapText=True, autoHeight=True)
    if "Status URL" in df.columns:
        gb.configure_column("Status URL", cellStyle=_STATUS_STYLE, maxWidth=170)
    if "Liputan Serupa" in df.columns:
        gb.configure_column("Liputan Serupa", cellStyle=_DUP_STYLE, maxWidth=140)
    if "URL" in df.columns:
        gb.configure_column("URL", cellRenderer=_LINK_RENDERER, headerName="Tautan", maxWidth=160)

    gridOptions = gb.build()

    AgGrid(
        df, gridOptions=gridOptions, theme="alpine",
        fit_columns_on_grid_load=False, suppressRowClickSelection=True, allow_unsafe_jscode=True,
    )

# ============================================================
# 6. SCRAPER (PARALEL + CACHE + DEDUP FUZZY + RETRY)
# ============================================================

@st.cache_data(ttl=3600, show_spinner=False)
def cached_gnews_search(keyword: str, start_date: dt.date, end_date: dt.date) -> List[Dict[str, Any]]:
    """Cache hasil Google News untuk 1 keyword + 1 periode (list dict ringan)."""
    all_entries: List[Dict[str, Any]] = []
    current_date = start_date

    while current_date < end_date:
        end_date_batch = min(current_date + DATE_DELTA, end_date)
        try:
            hasil = _gn_search(keyword, current_date.strftime("%Y-%m-%d"), end_date_batch.strftime("%Y-%m-%d"))
            for e in hasil.get("entries", []):
                title = getattr(e, "title", None) or e.get("title", "-") or "-"
                published = getattr(e, "published", None) or e.get("published", "") or ""
                link = getattr(e, "link", None) or e.get("link", "") or ""

                source_title = "-"
                try:
                    source_title = e.source.title
                except Exception:
                    try:
                        src = e.get("source", None)
                        if isinstance(src, dict):
                            source_title = src.get("title", "-") or "-"
                    except Exception:
                        source_title = "-"

                if link:
                    all_entries.append({"title": title, "published": published, "link": link, "source": source_title})
        except Exception as e:
            logger.warning("Pencarian gagal untuk '%s' (%s s/d %s): %s", keyword, current_date, end_date_batch, e)

        current_date = end_date_batch
        time.sleep(0.15 + random.uniform(0, 0.1))  # jeda kecil + jitter, hindari rate-limit

    return all_entries

@st.cache_data(ttl=24 * 3600, show_spinner=False)
def decode_url_once(gnews_link: str) -> Tuple[str, bool]:
    """Decode 1 link Google News jadi URL asli. Return (url, berhasil_decode)."""
    try:
        decoded = _decode(gnews_link)
        if decoded.get("status"):
            return decoded["decoded_url"], True
        return gnews_link, False
    except Exception as e:
        logger.debug("Decode gagal untuk %s: %s", gnews_link, e)
        return gnews_link, False

def parse_tanggal(published: str) -> Tuple[str, Optional[datetime]]:
    """Parse tanggal RSS (RFC 2822) -> (label tampilan, datetime untuk sorting/grafik)."""
    if not published:
        return "-", None
    try:
        pub_dt = parsedate_to_datetime(published)
        return pub_dt.strftime("%d %b %Y"), pub_dt
    except Exception:
        return published, None

def jalankan_scraper(
    WILAYAH: List[str],
    LAPANGAN_USAHA: List[str],
    START_DATE: dt.date,
    END_DATE: dt.date,
    decode_url: bool = True,
    max_workers_search: int = 10,
    max_workers_decode: int = 15,
):
    WILAYAH = [w.strip() for w in WILAYAH if str(w).strip()]
    LAPANGAN_USAHA = [u.strip() for u in LAPANGAN_USAHA if str(u).strip()]

    if not WILAYAH or not LAPANGAN_USAHA:
        st.warning("Wilayah dan/atau Lapangan Usaha kosong.")
        st.session_state.scraped_data = empty_result_df()
        return

    if START_DATE > END_DATE:
        st.error("⚠️ Tanggal mulai harus sebelum atau sama dengan tanggal akhir.")
        return

    t_start = time.time()
    combos: List[Tuple[str, str]] = list(itertools.product(WILAYAH, LAPANGAN_USAHA))

    progress = st.progress(0.0)
    status = st.empty()

    # 1) Pencarian paralel per kombinasi. Query pakai dua frasa dipisah spasi
    #    (AND implisit di Google News) — bukan "+" yang berarti karakter literal.
    done = 0
    results_raw: List[Tuple[str, str, List[Dict[str, Any]]]] = []

    with ThreadPoolExecutor(max_workers=max_workers_search) as ex:
        future_map = {}
        for (w, u) in combos:
            keyword = f'"{w}" "{u}"'
            fut = ex.submit(cached_gnews_search, keyword, START_DATE, END_DATE)
            future_map[fut] = (w, u)

        for fut in as_completed(future_map):
            w, u = future_map[fut]
            try:
                entries = fut.result() or []
            except Exception as e:
                logger.warning("Gagal memproses hasil untuk %s / %s: %s", w, u, e)
                entries = []
            results_raw.append((w, u, entries))
            done += 1
            progress.progress(done / max(1, len(combos)))
            status.write(f"🔎 Mencari berita: {done}/{len(combos)} kombinasi...")

    # 2) Dedup berdasarkan link + gabungkan wilayah/usaha yang menemukan link tsb
    by_link: Dict[str, Dict[str, Any]] = {}
    for w, u, entries in results_raw:
        for e in entries:
            link = e.get("link", "")
            if not link:
                continue
            tanggal_label, tanggal_dt = parse_tanggal(e.get("published", ""))
            if link not in by_link:
                by_link[link] = {
                    "Tanggal": tanggal_label,
                    "TanggalSort": tanggal_dt,
                    "Judul": e.get("title", "-") or "-",
                    "Sumber": e.get("source", "-") or "-",
                    "Wilayah": set([w]),
                    "Usaha": set([u]),
                    "GNewsLink": link,
                }
            else:
                by_link[link]["Wilayah"].add(w)
                by_link[link]["Usaha"].add(u)

    if not by_link:
        progress.empty()
        status.empty()
        st.warning("Tidak ada artikel ditemukan untuk kombinasi wilayah/usaha/periode ini.")
        st.session_state.scraped_data = empty_result_df()
        return

    status.write(f"🔗 Total artikel unik (sebelum decode URL): {len(by_link)}")

    # 3) Decode URL paralel (opsional)
    decoded_map: Dict[str, Tuple[str, bool]] = {}
    if decode_url:
        gnews_links = list(by_link.keys())
        done = 0
        progress.progress(0.0)
        with ThreadPoolExecutor(max_workers=max_workers_decode) as ex:
            future_map = {ex.submit(decode_url_once, ln): ln for ln in gnews_links}
            for fut in as_completed(future_map):
                ln = future_map[fut]
                try:
                    decoded_map[ln] = fut.result()
                except Exception as e:
                    logger.debug("Decode task gagal untuk %s: %s", ln, e)
                    decoded_map[ln] = (ln, False)
                done += 1
                progress.progress(done / max(1, len(gnews_links)))
                status.write(f"🔓 Decode URL: {done}/{len(gnews_links)} ...")
    else:
        decoded_map = {ln: (ln, False) for ln in by_link.keys()}

    # 4) Bangun record final
    records = []
    for gnews_link, obj in by_link.items():
        url_final, ok = decoded_map.get(gnews_link, (gnews_link, False))
        records.append({
            "Tanggal": obj["Tanggal"],
            "TanggalSort": obj["TanggalSort"],
            "Judul": obj["Judul"],
            "Sumber": obj["Sumber"],
            "Wilayah": ", ".join(sorted(obj["Wilayah"])),
            "Usaha": ", ".join(sorted(obj["Usaha"])),
            "Status URL": "Asli" if ok else "Redirect Google News",
            "URL": url_final,
        })

    df = pd.DataFrame(records)

    # 5) Deteksi liputan serupa lintas-portal via fuzzy title matching
    status.write("🧩 Mendeteksi liputan serupa antar media...")
    cluster_ids = cluster_similar_titles(df["Judul"].tolist())
    df["_cluster"] = cluster_ids
    cluster_sizes = df["_cluster"].value_counts()
    df["Liputan Serupa"] = df["_cluster"].map(lambda c: int(cluster_sizes[c]) - 1)
    df = df.drop(columns=["_cluster"])

    df = df.sort_values(by="TanggalSort", ascending=False, na_position="last").reset_index(drop=True)
    df = df[["Tanggal", "TanggalSort", "Judul", "Sumber", "Wilayah", "Usaha", "Liputan Serupa", "Status URL", "URL"]]

    st.session_state.scraped_data = df
    elapsed = time.time() - t_start

    # 6) Simpan ke riwayat (maks MAX_HISTORY entri terbaru)
    def _ringkas(items: List[str], n: int = 3) -> str:
        label = ", ".join(items[:n])
        return label + (f" +{len(items) - n} lainnya" if len(items) > n else "")

    st.session_state.setdefault("run_history", [])
    st.session_state.run_history.insert(0, {
        "waktu": datetime.now().strftime("%d %b %Y, %H:%M"),
        "wilayah": _ringkas(WILAYAH),
        "usaha": _ringkas(LAPANGAN_USAHA),
        "periode": f"{START_DATE:%d %b %Y} – {END_DATE:%d %b %Y}",
        "jumlah": len(df),
        "durasi": f"{elapsed:.1f}s",
        "df": df.copy(),
    })
    st.session_state.run_history = st.session_state.run_history[:MAX_HISTORY]

    progress.empty()
    status.empty()
    st.success(f"✅ Artikel terproses (unik): {len(df)} — selesai dalam {elapsed:.1f} detik")

def _split_manual(text: str) -> List[str]:
    parts: List[str] = []
    for line in text.splitlines():
        parts.extend(line.split(","))
    return [p.strip() for p in parts if p.strip()]

# ============================================================
# 7. HALAMAN & DATA REFERENSI
# ============================================================

@st.cache_data(ttl=3600, show_spinner=False)
def load_csv(url: str) -> pd.DataFrame:
    return _read_csv(url)

def sheet_csv_url(gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"

st.set_page_config(page_title="PAPEDA — Scraper Berita PDRB", page_icon="📊", layout="wide")

try:
    df_usaha = load_csv(sheet_csv_url(SHEET_USAHA_GID))
    df_wilayah = load_csv(sheet_csv_url(SHEET_WILAYAH_GID))
except Exception as e:
    logger.error("Gagal memuat referensi wilayah/usaha: %s", e)
    st.error(
        "⚠️ Gagal memuat daftar Wilayah/Lapangan Usaha dari Google Sheets. "
        "Periksa koneksi internet atau akses sheet, lalu muat ulang halaman."
    )
    st.stop()

daftar_usaha = df_usaha.columns.tolist()
daftar_wilayah = df_wilayah.columns.tolist()

if "scraped_data" not in st.session_state:
    st.session_state.scraped_data = empty_result_df()
if "run_history" not in st.session_state:
    st.session_state.run_history = []

# ============================================================
# 8. DESAIN (tipografi, warna, kartu, komponen)
# ============================================================

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

    :root {
        --primary-dark: #0D47A1; --primary: #1565C0; --primary-light: #2196F3; --accent: #26A69A;
        --bg: #F3F5F9; --card: #FFFFFF; --border: #E3E8EF; --text: #1E293B; --text-muted: #64748B;
        --danger: #DC2626; --radius: 14px;
        --shadow: 0 1px 3px rgba(15,23,42,.06), 0 1px 2px rgba(15,23,42,.04);
        --shadow-md: 0 8px 24px rgba(13,71,161,.14);
    }

    html, body, [class*="css"] { font-family: 'Inter', sans-serif !important; }

    .stApp, header[data-testid="stHeader"] { background: var(--bg) !important; color: var(--text) !important; }
    header[data-testid="stHeader"] { border-bottom: none !important; background: transparent !important; }
    .block-container { padding-top: 1.2rem !important; max-width: 1200px; }

    .stMarkdown, .stText, .stCaption, div[role="radiogroup"] * { color: var(--text) !important; }
    div[data-testid="stMarkdownContainer"] p { margin-bottom: 4px !important; }
    .stAlert div[role="alert"] { color: var(--text) !important; border-radius: 10px !important; }

    section[data-testid="stSidebar"] { background: var(--card) !important; border-right: 1px solid var(--border) !important; }
    section[data-testid="stSidebar"] * { color: var(--text) !important; }
    section[data-testid="stSidebar"] .block-container { padding-top: 1.5rem !important; }

    .hero-header {
        display: flex; align-items: center; gap: 24px;
        background: linear-gradient(135deg, var(--primary-dark) 0%, var(--primary-light) 100%);
        border-radius: var(--radius); padding: 26px 34px; box-shadow: var(--shadow-md); margin-bottom: 18px;
    }
    .hero-logo { height: 64px; width: 64px; object-fit: contain; border-radius: 10px; background: rgba(255,255,255,.92); padding: 8px; }
    .hero-title { color: #FFF !important; font-size: 30px; font-weight: 800; margin: 0; line-height: 1.1; letter-spacing: .5px; }
    .hero-subtitle { color: rgba(255,255,255,.88) !important; font-size: 14px; font-weight: 500; margin: 4px 0 10px 0; }
    .hero-badge {
        display: inline-flex; align-items: center; gap: 6px; background: rgba(255,255,255,.16); color: #FFF !important;
        border: 1px solid rgba(255,255,255,.3); border-radius: 999px; padding: 4px 12px; font-size: 12px; font-weight: 600;
    }

    button[data-baseweb="tab"] { font-weight: 600 !important; font-size: 14px !important; color: var(--text-muted) !important; }
    button[data-baseweb="tab"][aria-selected="true"] { color: var(--primary) !important; }
    div[data-baseweb="tab-highlight"] { background-color: var(--primary) !important; height: 3px !important; border-radius: 3px; }
    div[data-baseweb="tab-border"] { background-color: var(--border) !important; }

    div[data-testid="stVerticalBlockBorderWrapper"] {
        background: var(--card) !important; border: 1px solid var(--border) !important;
        border-radius: var(--radius) !important; box-shadow: var(--shadow) !important;
    }
    .section-title { font-size: 19px; font-weight: 700; color: var(--text) !important; margin: 0 0 2px 0; }
    .section-caption { font-size: 13px; color: var(--text-muted) !important; margin: 0 0 14px 0; }
    .field-label { font-size: 13.5px; font-weight: 600; color: var(--text) !important; margin-bottom: 6px; display: flex; align-items: center; gap: 6px; }

    div[data-baseweb="input"], div[data-baseweb="datepicker"], div[data-baseweb="textarea"], div[data-baseweb="select"] > div {
        border: 1px solid var(--border) !important; border-radius: 8px !important; background: #FBFCFE !important; font-size: 14px !important;
    }
    div[data-baseweb="input"], div[data-baseweb="select"] > div { min-height: 46px !important; padding: 4px 10px !important; display: flex; align-items: center; }
    div[data-baseweb="input"] input, div[data-baseweb="datepicker"] input, div[data-baseweb="textarea"] textarea, div[data-baseweb="select"] span {
        background: transparent !important; color: var(--text) !important; font-size: 14px !important;
    }
    div[data-baseweb="input"]:focus-within, div[data-baseweb="select"] > div:focus-within, div[data-baseweb="textarea"]:focus-within {
        border-color: var(--primary-light) !important; box-shadow: 0 0 0 3px rgba(33,150,243,.15) !important;
    }
    div[data-baseweb="popover"] { background: var(--card) !important; color: var(--text) !important; font-size: 14px !important; border-radius: 10px !important; }
    span[data-baseweb="tag"] { background: var(--primary) !important; border-radius: 6px !important; }

    div[role="radiogroup"] {
        display: inline-flex; background: #EEF2F8; border-radius: 8px; padding: 3px; gap: 2px;
        margin-top: 8px !important; margin-bottom: 4px !important; border: none;
    }
    div[role="radiogroup"] label { margin: 0 !important; padding: 4px 14px !important; border-radius: 6px !important; font-size: 12.5px !important; font-weight: 600 !important; cursor: pointer; }
    div[role="radiogroup"] label:has(input:checked) { background: var(--card) !important; box-shadow: var(--shadow) !important; color: var(--primary) !important; }

    div.stButton > button {
        background: var(--primary) !important; color: #FFF !important; border-radius: 9px !important; border: none !important;
        padding: 10px 18px !important; font-weight: 600 !important; font-size: 14px !important;
        transition: background .15s ease, transform .05s ease;
    }
    div.stButton > button:hover { background: var(--primary-dark) !important; }
    div.stButton > button:active { transform: scale(.98); }
    div.stButton > button[kind="secondary"] { background: #FFF !important; color: var(--danger) !important; border: 1px solid var(--border) !important; }
    div.stButton > button[kind="secondary"]:hover { background: #FEF2F2 !important; border-color: var(--danger) !important; }

    div.stDownloadButton > button {
        background: var(--primary) !important; color: #FFF !important; font-weight: 600 !important;
        border-radius: 9px !important; padding: 9px 16px !important; border: none !important;
    }
    div.stDownloadButton > button:hover { background: var(--primary-dark) !important; }

    div[data-testid="stCheckbox"] label p { font-size: 13.5px !important; color: var(--text-muted) !important; }

    .kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin: 4px 0 18px 0; }
    .kpi-card { background: var(--card); border: 1px solid var(--border); border-left: 4px solid var(--kpi-accent, var(--primary)); border-radius: 12px; padding: 14px 18px; box-shadow: var(--shadow); }
    .kpi-icon { font-size: 20px; }
    .kpi-value { font-size: 26px; font-weight: 800; color: var(--text); line-height: 1.2; margin-top: 2px; }
    .kpi-label { font-size: 12.5px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: .4px; }

    .empty-state { text-align: center; padding: 46px 20px; color: var(--text-muted); }
    .empty-state-icon { font-size: 38px; margin-bottom: 8px; }
    .empty-state-title { font-size: 16px; font-weight: 700; color: var(--text); margin-bottom: 4px; }

    .hist-meta { font-size: 12.5px; color: var(--text-muted); margin-top: 2px; }
    .hist-title { font-size: 14.5px; font-weight: 700; color: var(--text); }
    .hist-badge { display: inline-block; background: #EEF2F8; color: var(--primary); border-radius: 6px; padding: 2px 8px; font-size: 12px; font-weight: 700; margin-left: 6px; }

    .app-footer { text-align: center; color: var(--text-muted); font-size: 12.5px; padding: 20px 0 8px 0; }
    </style>
    """,
    unsafe_allow_html=True
)

# ============================================================
# 9. SIDEBAR — pengaturan lanjutan
# ============================================================

_logo_path = os.path.join(os.path.dirname(__file__), "Logo.png")
_logo_html = ""
if os.path.exists(_logo_path):
    with open(_logo_path, "rb") as f:
        encoded_logo = base64.b64encode(f.read()).decode()
    _logo_html = f'<img src="data:image/png;base64,{encoded_logo}" class="hero-logo" />'

with st.sidebar:
    st.markdown("### ⚙️ Pengaturan Lanjutan")
    st.caption("Sesuaikan kecepatan vs. risiko rate-limit dari Google News.")
    max_workers_search = st.slider("Paralel pencarian", 4, 20, 10, help="Jumlah kombinasi wilayah×usaha yang dicari bersamaan.")
    max_workers_decode = st.slider("Paralel decode URL", 4, 30, 15, help="Jumlah URL yang di-decode bersamaan.")

    st.markdown("---")
    if st.button("🧹 Bersihkan Cache", use_container_width=True):
        st.cache_data.clear()
        st.toast("Cache dibersihkan.", icon="🧹")

    st.markdown("---")
    st.caption(f"Referensi wilayah/usaha dari Google Sheets · {len(daftar_wilayah)} kelompok wilayah, {len(daftar_usaha)} kelompok usaha.")
    st.caption("PAPEDA v2.1 · Tanpa AI/ringkasan otomatis — data mentah untuk analisis manual.")

# ============================================================
# 10. HERO HEADER
# ============================================================

st.markdown(
    f"""
    <div class="hero-header">
        {_logo_html}
        <div>
            <h1 class="hero-title">PAPEDA</h1>
            <div class="hero-subtitle">Pengumpulan Analisis Perkembangan Ekonomi Daerah</div>
            <span class="hero-badge">📊 Monitoring Berita Fenomena PDRB</span>
        </div>
    </div>
    """,
    unsafe_allow_html=True
)

# ============================================================
# 11. TABS UTAMA
# ============================================================

tab_cari, tab_analisis, tab_riwayat = st.tabs(["🔍 Pencarian", "📈 Analisis", "🗂️ Riwayat"])

# ---------- TAB 1: PENCARIAN ----------
with tab_cari:
    with st.container(border=True):
        st.markdown("<div class='section-title'>Parameter Pencarian</div>", unsafe_allow_html=True)
        st.markdown(
            "<div class='section-caption'>Pilih satu atau beberapa wilayah & lapangan usaha, lalu tentukan periode berita.</div>",
            unsafe_allow_html=True
        )

        col1, col2, col3 = st.columns(3, gap="large")

        with col1:
            st.markdown("<div class='field-label'>📍 Wilayah</div>", unsafe_allow_html=True)
            if st.session_state.get("wilayah_mode", "Opsi") == "Opsi":
                wilayah_pilihan = st.multiselect("Pilih Wilayah", daftar_wilayah, default=daftar_wilayah[:1], label_visibility="collapsed")
            else:
                wilayah_manual = st.text_area(
                    "Masukkan Wilayah Manual", "", height=46, label_visibility="collapsed",
                    placeholder="Satu wilayah per baris atau pisahkan dengan koma"
                )
            wilayah_mode = st.radio("Metode Input Wilayah", ["Opsi", "Manual"], horizontal=True, key="wilayah_mode", label_visibility="collapsed")

        with col2:
            st.markdown("<div class='field-label'>🏭 Lapangan Usaha</div>", unsafe_allow_html=True)
            if st.session_state.get("usaha_mode", "Opsi") == "Opsi":
                usaha_pilihan = st.multiselect("Pilih Lapangan Usaha", daftar_usaha, default=daftar_usaha[:1], label_visibility="collapsed")
            else:
                usaha_manual = st.text_area(
                    "Masukkan Usaha Manual", "", height=46, label_visibility="collapsed",
                    placeholder="Satu lapangan usaha per baris atau pisahkan dengan koma"
                )
            usaha_mode = st.radio("Metode Input Usaha", ["Opsi", "Manual"], horizontal=True, key="usaha_mode", label_visibility="collapsed")

        with col3:
            st.markdown("<div class='field-label'>📅 Periode Tanggal</div>", unsafe_allow_html=True)
            periode = st.date_input(
                "", label_visibility="collapsed", key="Tanggal",
                value=(dt.date(2025, 8, 19), dt.date(2025, 8, 28)), format="YYYY-MM-DD"
            )
            if isinstance(periode, tuple) and len(periode) == 2:
                start_date, end_date = periode
            else:
                st.error("⚠️ Harap pilih rentang tanggal.")
                start_date, end_date = dt.date.today() - dt.timedelta(days=7), dt.date.today()
            decode_url_toggle = st.checkbox("Decode URL asli (lebih lambat, hasil link lebih rapi)", value=True)

        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
        col_scrape, col_reset = st.columns([5, 1])
        with col_scrape:
            scrape_button = st.button("🔍 Mulai Scraping", key="scrape_button", use_container_width=True)
        with col_reset:
            reset_button = st.button("🗑️ Reset", key="reset_button", help="Hapus hasil saat ini", type="secondary", use_container_width=True)

    if reset_button:
        st.session_state.scraped_data = empty_result_df()
        st.rerun()

    if scrape_button:
        if wilayah_mode == "Opsi":
            WILAYAH: List[str] = []
            for key in wilayah_pilihan:
                WILAYAH.extend(df_wilayah[key].dropna().astype(str).tolist() if key in df_wilayah.columns else [key])
        else:
            WILAYAH = _split_manual(wilayah_manual)

        if usaha_mode == "Opsi":
            LAPANGAN_USAHA: List[str] = []
            for key in usaha_pilihan:
                LAPANGAN_USAHA.extend(df_usaha[key].dropna().astype(str).tolist() if key in df_usaha.columns else [key])
        else:
            LAPANGAN_USAHA = _split_manual(usaha_manual)

        jalankan_scraper(
            WILAYAH=WILAYAH,
            LAPANGAN_USAHA=LAPANGAN_USAHA,
            START_DATE=start_date,
            END_DATE=end_date,
            decode_url=decode_url_toggle,
            max_workers_search=max_workers_search,
            max_workers_decode=max_workers_decode,
        )

    df_result = st.session_state.scraped_data
    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    if df_result.empty:
        with st.container(border=True):
            st.markdown(
                """
                <div class="empty-state">
                    <div class="empty-state-icon">📊</div>
                    <div class="empty-state-title">Belum ada data</div>
                    <div>Pilih wilayah, lapangan usaha, dan periode di atas, lalu klik <b>Mulai Scraping</b>.</div>
                </div>
                """,
                unsafe_allow_html=True
            )
    else:
        total_wilayah = len(set(", ".join(df_result["Wilayah"]).split(", ")))
        total_usaha = len(set(", ".join(df_result["Usaha"]).split(", ")))
        st.markdown(
            f"""
            <div class="kpi-row">
                <div class="kpi-card" style="--kpi-accent:#1565C0">
                    <div class="kpi-icon">📰</div><div class="kpi-value">{len(df_result)}</div><div class="kpi-label">Total Artikel</div>
                </div>
                <div class="kpi-card" style="--kpi-accent:#26A69A">
                    <div class="kpi-icon">🏷️</div><div class="kpi-value">{df_result["Sumber"].nunique()}</div><div class="kpi-label">Sumber Berita</div>
                </div>
                <div class="kpi-card" style="--kpi-accent:#7C3AED">
                    <div class="kpi-icon">📍</div><div class="kpi-value">{total_wilayah}</div><div class="kpi-label">Wilayah Tercakup</div>
                </div>
                <div class="kpi-card" style="--kpi-accent:#F59E0B">
                    <div class="kpi-icon">🏭</div><div class="kpi-value">{total_usaha}</div><div class="kpi-label">Usaha Tercakup</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )

        with st.container(border=True):
            colh1, colh2, colh3 = st.columns([7, 1.5, 1.5])
            with colh1:
                st.markdown("<div class='section-title'>Hasil Scraping</div>", unsafe_allow_html=True)
            with colh2:
                st.download_button("⬇️ Excel", data=to_excel(df_result), file_name="hasil_scraping.xlsx",
                                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
            with colh3:
                st.download_button("⬇️ CSV", data=to_csv(df_result), file_name="hasil_scraping.csv",
                                    mime="text/csv", use_container_width=True)
            st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
            show_aggrid(df_result)

# ---------- TAB 2: ANALISIS ----------
with tab_analisis:
    df_result = st.session_state.scraped_data
    if df_result.empty:
        with st.container(border=True):
            st.markdown(
                """
                <div class="empty-state">
                    <div class="empty-state-icon">📈</div>
                    <div class="empty-state-title">Belum ada data untuk dianalisis</div>
                    <div>Jalankan pencarian di tab <b>Pencarian</b> terlebih dahulu.</div>
                </div>
                """,
                unsafe_allow_html=True
            )
    else:
        with st.container(border=True):
            st.markdown("<div class='section-title'>Tren Pemberitaan Harian</div>", unsafe_allow_html=True)
            st.markdown("<div class='section-caption'>Lonjakan jumlah artikel per hari sering menandai fenomena ekonomi yang layak ditelusuri lebih lanjut.</div>", unsafe_allow_html=True)
            harian = df_result.dropna(subset=["TanggalSort"]).copy()
            if not harian.empty:
                harian["Hari"] = pd.to_datetime(harian["TanggalSort"]).dt.date
                chart_area(harian.groupby("Hari").size().sort_index(), color="#1565C0")
            else:
                chart_area(pd.Series(dtype=int), color="#1565C0")

        colA, colB = st.columns(2, gap="large")
        with colA:
            with st.container(border=True):
                st.markdown("<div class='section-title'>Sumber Berita Teratas</div>", unsafe_allow_html=True)
                st.markdown("<div class='section-caption'>10 media dengan jumlah artikel terbanyak.</div>", unsafe_allow_html=True)
                chart_bar_h(explode_counts(df_result["Sumber"]), color="#26A69A")
        with colB:
            with st.container(border=True):
                st.markdown("<div class='section-title'>Distribusi per Wilayah</div>", unsafe_allow_html=True)
                st.markdown("<div class='section-caption'>10 wilayah dengan liputan terbanyak.</div>", unsafe_allow_html=True)
                chart_bar_h(explode_counts(df_result["Wilayah"]), color="#7C3AED")

        colC, colD = st.columns(2, gap="large")
        with colC:
            with st.container(border=True):
                st.markdown("<div class='section-title'>Distribusi per Lapangan Usaha</div>", unsafe_allow_html=True)
                st.markdown("<div class='section-caption'>10 lapangan usaha dengan liputan terbanyak.</div>", unsafe_allow_html=True)
                chart_bar_h(explode_counts(df_result["Usaha"]), color="#F59E0B")
        with colD:
            with st.container(border=True):
                st.markdown("<div class='section-title'>Kata Kunci Trending</div>", unsafe_allow_html=True)
                st.markdown("<div class='section-caption'>Kata yang paling sering muncul di judul (di luar wilayah/usaha yang dicari).</div>", unsafe_allow_html=True)
                exclude_terms = set()
                for col in ["Wilayah", "Usaha"]:
                    for val in df_result[col].dropna().astype(str):
                        exclude_terms.update(re.findall(r"[a-zA-Z]{3,}", val.lower()))
                chart_bar_h(extract_keywords(df_result["Judul"].tolist(), exclude_terms), color="#EC4899")

        n_liputan_luas = int((df_result["Liputan Serupa"] > 0).sum())
        if n_liputan_luas:
            st.info(f"📌 {n_liputan_luas} artikel terindikasi memiliki liputan serupa dari portal lain (judul mirip, dideteksi dengan fuzzy matching) — sinyal berita tersebut mendapat perhatian media luas. Lihat kolom **Liputan Serupa** pada tabel hasil.")

# ---------- TAB 3: RIWAYAT ----------
with tab_riwayat:
    history = st.session_state.get("run_history", [])
    if not history:
        with st.container(border=True):
            st.markdown(
                """
                <div class="empty-state">
                    <div class="empty-state-icon">🗂️</div>
                    <div class="empty-state-title">Belum ada riwayat pencarian</div>
                    <div>Riwayat akan tersimpan otomatis setiap kali Anda menjalankan scraping (maksimal 8 terbaru).</div>
                </div>
                """,
                unsafe_allow_html=True
            )
    else:
        col_info, col_clear = st.columns([5, 1])
        with col_info:
            st.caption(f"Menampilkan {len(history)} pencarian terakhir.")
        with col_clear:
            if st.button("🗑️ Hapus Semua", use_container_width=True, type="secondary"):
                st.session_state.run_history = []
                st.rerun()

        for i, h in enumerate(history):
            with st.container(border=True):
                c1, c2 = st.columns([6, 1])
                with c1:
                    st.markdown(
                        f"""
                        <div class="hist-title">{h['waktu']} <span class="hist-badge">{h['jumlah']} artikel</span></div>
                        <div class="hist-meta">📍 {h['wilayah']} &nbsp;·&nbsp; 🏭 {h['usaha']} &nbsp;·&nbsp; 📅 {h['periode']} &nbsp;·&nbsp; ⏱️ {h['durasi']}</div>
                        """,
                        unsafe_allow_html=True
                    )
                with c2:
                    if st.button("🔄 Muat", key=f"load_hist_{i}", use_container_width=True):
                        st.session_state.scraped_data = h["df"]
                        st.rerun()

# ============================================================
# 12. FOOTER
# ============================================================

st.markdown(
    "<div class='app-footer'>PAPEDA · Alat bantu monitoring berita fenomena ekonomi daerah (PDRB) · Sumber data: Google News</div>",
    unsafe_allow_html=True
)
