# ============================================================
# PAPEDA - Scraper Berita PDRB (CEPAT, TANPA GEMINI & TANPA RINGKASAN)
# Output: Tanggal, Judul, Sumber, Wilayah, Usaha, URL
# ============================================================

import streamlit as st
import time
import itertools
from typing import List, Dict, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import datetime as dt
import base64
import pandas as pd
from pygooglenews import GoogleNews
from googlenewsdecoder import gnewsdecoder
from st_aggrid import AgGrid, GridOptionsBuilder
from io import BytesIO

# ============================================================
# 1. KONFIGURASI & VARIABEL GLOBAL
# ============================================================

gn = GoogleNews(lang="id")
DATE_DELTA = dt.timedelta(days=30)

# ============================================================
# 2. CSS (DOWNLOAD BUTTON) - SAMA SEPERTI SEBELUMNYA
# ============================================================

st.markdown(
    """
    <style>
    div.stDownloadButton > button {
        background-color: #2196F3;
        color: white; font-weight: bold;
        border-radius: 8px;
        padding: 0.5em 1em;
    }
    div.stDownloadButton > button:hover {
        background-color: #1565C0;
    }
    </style>
    """,
    unsafe_allow_html=True
)

# ============================================================
# 3. EXPORT EXCEL
# ============================================================

def to_excel(df: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Data")
    return output.getvalue()

# ============================================================
# 4. TABEL (AgGrid) - SAMA SEPERTI SEBELUMNYA
# ============================================================

def show_aggrid(df: pd.DataFrame):
    df = df.reset_index(drop=True)
    if "index" in df.columns:
        df = df.drop(columns=["index"])

    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_pagination(paginationPageSize=10)
    gb.configure_side_bar()
    gb.configure_default_column(editable=False, groupable=True)
    gb.configure_grid_options(
        enableRangeSelection=True,
        enableCellTextSelection=True
    )
    gb.configure_selection("multiple", use_checkbox=False)
    gridOptions = gb.build()

    col1, col2 = st.columns([8, 2])
    with col1:
        st.markdown(
            """
            <div style='display:flex; align-items:center; height:40px;'>
                <h3 style='margin:0; font-size:26px;'>Hasil Sementara</h3>
            </div>
            """,
            unsafe_allow_html=True
        )
    with col2:
        st.download_button(
            "⬇️ Download Excel",
            data=to_excel(df),
            file_name="hasil_scraping.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

    AgGrid(
        df,
        gridOptions=gridOptions,
        theme="light",
        fit_columns_on_grid_load=False,
        suppressRowClickSelection=True
    )

# ============================================================
# 5. SCRAPER CEPAT (PARALEL + CACHE + DEDUP)
# ============================================================

@st.cache_data(ttl=3600, show_spinner=False)
def cached_gnews_search(keyword: str, start_date: dt.date, end_date: dt.date) -> List[Dict[str, Any]]:
    """
    Cache hasil Google News untuk 1 keyword + 1 periode.
    Disimpan sebagai list dict minimal (ringan & aman untuk cache).
    """
    all_entries: List[Dict[str, Any]] = []
    current_date = start_date

    while current_date < end_date:
        end_date_batch = min(current_date + DATE_DELTA, end_date)
        try:
            hasil = gn.search(
                keyword,
                from_=current_date.strftime("%Y-%m-%d"),
                to_=end_date_batch.strftime("%Y-%m-%d")
            )
            for e in hasil.get("entries", []):
                # pygooglenews entry biasanya object
                title = getattr(e, "title", None) or e.get("title", "-") or "-"
                published = getattr(e, "published", None) or e.get("published", "") or ""
                link = getattr(e, "link", None) or e.get("link", "") or ""

                # source bisa object/dict
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
                    all_entries.append({
                        "title": title,
                        "published": published,
                        "link": link,
                        "source": source_title
                    })
        except Exception:
            pass

        current_date = end_date_batch
        time.sleep(0.15)  # lebih cepat dari 1 detik

    return all_entries

@st.cache_data(ttl=24*3600, show_spinner=False)
def decode_url_once(gnews_link: str) -> str:
    """
    Decode 1 link Google News jadi URL asli. Dicache supaya tidak mengulang.
    """
    try:
        decoded = gnewsdecoder(gnews_link)
        return decoded["decoded_url"] if decoded.get("status") else gnews_link
    except Exception:
        return gnews_link

def parse_tanggal_str(published: str) -> str:
    """
    Format tanggal output dd Mon YYYY kalau bisa. Kalau tidak, kembalikan mentah.
    """
    if not published:
        return "-"
    try:
        pub_dt = datetime.strptime(published, "%a, %d %b %Y %H:%M:%S %Z")
        return pub_dt.strftime("%d %b %Y")
    except Exception:
        return published

def jalankan_scraper_streamlit_cepat(
    WILAYAH: List[str],
    LAPANGAN_USAHA: List[str],
    START_DATE: dt.date,
    END_DATE: dt.date,
    decode_url: bool = True,
    max_workers_search: int = 10,
    max_workers_decode: int = 20
):
    WILAYAH = [w.strip() for w in WILAYAH if str(w).strip()]
    LAPANGAN_USAHA = [u.strip() for u in LAPANGAN_USAHA if str(u).strip()]

    if not WILAYAH or not LAPANGAN_USAHA:
        st.warning("Wilayah dan/atau Lapangan Usaha kosong.")
        st.session_state.scraped_data = pd.DataFrame(columns=["Tanggal","Judul","Sumber","Wilayah","Usaha","URL"])
        return

    combos: List[Tuple[str, str]] = list(itertools.product(WILAYAH, LAPANGAN_USAHA))

    progress = st.progress(0.0)
    status = st.empty()

    # 1) Search paralel per kombinasi
    done = 0
    results_raw: List[Tuple[str, str, List[Dict[str, Any]]]] = []

    with ThreadPoolExecutor(max_workers=max_workers_search) as ex:
        future_map = {}
        for (w, u) in combos:
            keyword = f'"{w}"+"{u}"'
            fut = ex.submit(cached_gnews_search, keyword, START_DATE, END_DATE)
            future_map[fut] = (w, u)

        for fut in as_completed(future_map):
            w, u = future_map[fut]
            try:
                entries = fut.result() or []
            except Exception:
                entries = []
            results_raw.append((w, u, entries))

            done += 1
            progress.progress(done / max(1, len(combos)))
            status.write(f"🔎 Mencari berita: {done}/{len(combos)} kombinasi...")

    # 2) DEDUP berdasarkan link + gabungkan wilayah/usaha yang menemukan link tsb
    by_link: Dict[str, Dict[str, Any]] = {}
    for w, u, entries in results_raw:
        for e in entries:
            link = e.get("link", "")
            if not link:
                continue

            if link not in by_link:
                by_link[link] = {
                    "Tanggal": parse_tanggal_str(e.get("published", "")),
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
        st.warning("Tidak ada artikel ditemukan.")
        st.session_state.scraped_data = pd.DataFrame(columns=["Tanggal","Judul","Sumber","Wilayah","Usaha","URL"])
        return

    status.write(f"🔗 Total artikel unik (sebelum decode URL): {len(by_link)}")

    # 3) Decode URL paralel (opsional)
    decoded_map: Dict[str, str] = {}
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
                except Exception:
                    decoded_map[ln] = ln

                done += 1
                progress.progress(done / max(1, len(gnews_links)))
                status.write(f"🔓 Decode URL: {done}/{len(gnews_links)} ...")
    else:
        decoded_map = {ln: ln for ln in by_link.keys()}

    # 4) Build records final
    records = []
    for gnews_link, obj in by_link.items():
        records.append({
            "Tanggal": obj["Tanggal"],
            "Judul": obj["Judul"],
            "Sumber": obj["Sumber"],
            "Wilayah": ", ".join(sorted(obj["Wilayah"])),
            "Usaha": ", ".join(sorted(obj["Usaha"])),
            "URL": decoded_map.get(gnews_link, gnews_link),
        })

    df = pd.DataFrame(records)
    st.session_state.scraped_data = df

    progress.empty()
    status.empty()
    st.success(f"✅ Artikel terproses (unik): {len(df)}")

# ============================================================
# 6. STREAMLIT UI - TAMPILAN SAMA PERSIS DENGAN SEBELUMNYA
# ============================================================

# --- A. Input Data Wilayah dan Lapangan Usaha ---
@st.cache_data(ttl=3600, show_spinner=False)
def load_csv(url: str) -> pd.DataFrame:
    return pd.read_csv(url)

df_usaha = load_csv("https://docs.google.com/spreadsheets/d/1cSISqNtyiGiyZ4nqTrTxBWIO7U98RBS5Z9ehBMWadYo/export?format=csv&gid=233383135")
df_wilayah = load_csv("https://docs.google.com/spreadsheets/d/1cSISqNtyiGiyZ4nqTrTxBWIO7U98RBS5Z9ehBMWadYo/export?format=csv&gid=0")

daftar_usaha = df_usaha.columns.tolist()
daftar_wilayah = df_wilayah.columns.tolist()

# --- B. Konfigurasi halaman ---
st.set_page_config(page_title="Scraper Berita PDRB", layout="wide")

# --- C. CSS custom (SAMA) ---
st.markdown(
    """
    <style>
        /* ===== Global ===== */
        .stApp, header[data-testid="stHeader"] { background: #FFF !important; color: #000 !important; border-bottom: 3px solid #e7dfdd !important; height: }
        header[data-testid="stHeader"] *,
        .stMarkdown, .stText, .stTitle, .stSubheader, .stHeader, .stCaption,
        div[role="radiogroup"] * { color: #000 !important; }

        /* Alert */
        .stAlert div[role="alert"] { color: #000 !important; }

        /* Spacing */
        div[data-testid="stMarkdownContainer"] p { margin-bottom: 4px !important; }
        div[data-testid="stVerticalBlock"] > div { margin-bottom: 0 !important; }
        div[role="radiogroup"] { margin-top: -12px !important; }
        .block-container { padding-top: 0rem !important; }

        /* ===== Input / Datepicker / Select (base style) ===== */
        div[data-baseweb="input"],
        div[data-baseweb="datepicker"],
        div[data-baseweb="select"] > div {
            height: 50px !important;
            min-height: 38px !important;
            border: 1px solid #ccc !important;
            border-radius: 6px !important;
            background: #FFF !important;
            padding: 4px 10px !important;
            display: flex; align-items: center;
            font-size: 14px !important; line-height: 1.4 !important;
        }

        /* Teks di dalam kontrol */
        div[data-baseweb="input"] input,
        div[data-baseweb="datepicker"] input,
        div[data-baseweb="select"] span {
            background: #FFF !important; color: #000 !important; font-size: 14px !important;
        }

        /* Dropdown popover */
        div[data-baseweb="popover"] { background: #FFF !important; color: #000 !important; font-size: 14px !important; }

        /* Tombol */
        div.stButton > button {
            background: #2196F3 !important; color: #FFF !important;
            border-radius: 6px !important; border: none; padding: 8px 18px !important;
        }
        div.stButton > button:hover { background: #1565C0 !important; }

        /* Filter Container */
        div[data-testid="stHorizontalBlock"] {
            border: 1px solid #ccc; border-radius: 10px;
            padding: 20px 15px 10px; margin-top: 20px; background: #FFF;
        }

        /* Judul */
        .centered-title {
            text-align: center; font-size: 37px !important; font-weight: bold;
            margin: 0 !important;
            line-height: 1; }
        .centered-subtitle {
            text-align: center;
            font-size: 23px !important;
            font-weight: normal;
            margin: 0 !important;
            color: #555;
            line-height: 1; }

        /* ===== FINAL OVERRIDE (HARUS DI BAWAH) =====*/
        div[data-baseweb="select"] > div {
            color: #000 !important;
            background: #FFF !important;
            padding: 4px 8px !important;
            line-height: 1.4 !important;
        }
    </style>
    """,
    unsafe_allow_html=True
)

# --- D. Logo (SAMA) ---
with open("Logo.png", "rb") as f:
    encoded = base64.b64encode(f.read()).decode()

st.markdown(
    f"""
    <style>
        [data-testid="stHeader"]::before {{
            content: "";
            position: absolute;
            top: -10px; left: 0px;
            height: 235px; width: 235px;
            background-image: url("data:image/png;base64,{encoded}");
            background-size: contain;
            background-repeat: no-repeat;
        }}
    </style>
    """,
    unsafe_allow_html=True
)

# --- E. Judul (SAMA) ---
st.markdown("<div style='padding-top:30px'></div>", unsafe_allow_html=True)
st.markdown(
    """
    <h1 class='centered-title'>PAPEDA</h1>
    <div class='centered-subtitle'>Pengumpulan Analisis Perkembangan Ekonomi Daerah</div>
    """,
    unsafe_allow_html=True
)
st.markdown("<div style='padding-top:15px'></div>", unsafe_allow_html=True)

# --- F. Kotak Input (SAMA) ---
col1, col2, _, col3, _, col4, col5 = st.columns([0.8, 4, 0.2, 4, 0.2, 4, 0.8])

with col2:
    st.markdown("**Wilayah**")
    if st.session_state.get("wilayah_mode", "Opsi") == "Opsi":
        wilayah_input = st.selectbox("Pilih Wilayah", daftar_wilayah, index=0, label_visibility="collapsed")
    else:
        wilayah_input = st.text_input("Masukkan Wilayah Manual", "", label_visibility="collapsed")
    wilayah_mode = st.radio(
        "Metode Input Wilayah",
        ["Opsi", "Manual"],
        horizontal=True,
        key="wilayah_mode",
        label_visibility="collapsed"
    )
    scrape_button = st.button("🔍 Mulai Scraping", key="scrape_button")

with col3:
    st.markdown("**Lapangan Usaha**")
    if st.session_state.get("usaha_mode", "Opsi") == "Opsi":
        usaha_input = st.selectbox("Pilih Lapangan Usaha", daftar_usaha, index=0, label_visibility="collapsed")
    else:
        usaha_input = st.text_input("Masukkan Usaha Manual", "", label_visibility="collapsed")
    usaha_mode = st.radio(
        "Metode Input Usaha",
        ["Opsi", "Manual"],
        horizontal=True,
        key="usaha_mode",
        label_visibility="collapsed"
    )

with col4:
    st.markdown("**Periode Tanggal**")
    periode = st.date_input(
        "",
        label_visibility="collapsed",
        key="Tanggal",
        value=(dt.date(2025, 8, 19), dt.date(2025, 8, 28)),
        format="YYYY-MM-DD"
    )
    if isinstance(periode, tuple) and len(periode) == 2:
        start_date, end_date = periode
    else:
        st.error("⚠️ Harap pilih rentang tanggal.")
        start_date, end_date = dt.date.today() - dt.timedelta(days=7), dt.date.today()

# Toggle decode URL (cepat vs akurat)
# (Ini tidak mengubah tampilan utama; cuma opsi kecil tambahan di bawah filter.)
decode_url_toggle = st.checkbox("Decode URL asli (lebih lambat)", value=True)

# --- G. DataFrame Kosong Awal ---
if "scraped_data" not in st.session_state:
    st.session_state.scraped_data = pd.DataFrame(
        columns=["Tanggal", "Judul", "Sumber", "Wilayah", "Usaha", "URL"]
    )

# --- H. Jalankan Scraper (VERSI CEPAT) ---
if scrape_button:
    # Wilayah
    if wilayah_input:
        wilayah_key = wilayah_input.strip()
        if wilayah_mode == "Opsi" and wilayah_key in df_wilayah.columns:
            WILAYAH = df_wilayah[wilayah_key].dropna().astype(str).tolist()
        else:
            WILAYAH = [wilayah_key]
    else:
        WILAYAH = []

    # Usaha
    if usaha_input:
        usaha_key = usaha_input.strip()
        if usaha_mode == "Opsi" and usaha_key in df_usaha.columns:
            LAPANGAN_USAHA = df_usaha[usaha_key].dropna().astype(str).tolist()
        else:
            LAPANGAN_USAHA = [usaha_key]
    else:
        LAPANGAN_USAHA = []

    jalankan_scraper_streamlit_cepat(
        WILAYAH=WILAYAH,
        LAPANGAN_USAHA=LAPANGAN_USAHA,
        START_DATE=start_date,
        END_DATE=end_date,
        decode_url=decode_url_toggle,
        max_workers_search=10,
        max_workers_decode=20
    )

# --- I. Tampilkan Data ---
show_aggrid(st.session_state.scraped_data)
