# ============================================================
# APP STREAMLIT UNTUK WEB SCRAPING PDRB
# ============================================================
import streamlit as st
import google.generativeai as genai
import requests, trafilatura, re, time, itertools
from typing import List, Dict, Optional
from pygooglenews import GoogleNews
from googlenewsdecoder import gnewsdecoder
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import base64, xlsxwriter, openpyxl
import pandas as pd
from st_aggrid import AgGrid, GridOptionsBuilder
from io import BytesIO

# ============================================================
# 1. KONFIGURASI & VARIABEL GLOBAL
# ============================================================

# --- A. Konfigurasi Gemini ---

# --- A. Konfigurasi Gemini ---
API_KEYS = st.secrets["API_KEYS"]
current_key_idx = 0

# Ambil model dengan rotasi key
def get_rotating_model():
    global current_key_idx
    key = API_KEYS[current_key_idx]
    current_key_idx = (current_key_idx + 1) % len(API_KEYS)
    genai.configure(api_key=key)
    return genai.GenerativeModel("gemini-2.0-flash")

# --- B. Inisialisasi Google News ---
gn = GoogleNews(lang='id')

# --- C. Variabel default ---
DEFAULT_WILAYAH = []
DEFAULT_LAPANGAN_USAHA = []
DATE_DELTA = dt.timedelta(days=30)
SEEN_URLS = set()

# ============================================================
# 2. LAYANAN EKSTERNAL
# ============================================================

# --- A. Mencari Artikel Google News ---
def cari_artikel_google_news(keyword: str, START_DATE: dt.date, END_DATE: dt.date) -> List[dict]:
    all_entries, current_date = [], START_DATE
    while current_date < END_DATE:
        end_date_batch = min(current_date + DATE_DELTA, END_DATE)
        try:
            hasil = gn.search(
                keyword,
                from_=current_date.strftime("%Y-%m-%d"),
                to_=end_date_batch.strftime("%Y-%m-%d")
            )
            for entry in hasil["entries"]:
                if entry.link not in SEEN_URLS:
                    SEEN_URLS.add(entry.link)
                    all_entries.append(entry)
        except Exception as e:
            st.warning(f"⚠️ Gagal cari berita {current_date}: {e}")
        current_date = end_date_batch
        time.sleep(1)
    return all_entries

# --- B. Ambil Url Asli ---
def ambil_url_asli(entry: dict) -> str:
    try:
        decoded = gnewsdecoder(entry.link)
        return decoded["decoded_url"] if decoded.get("status") else entry.link
    except:
        return entry.link

# --- C. Ringkasan Gemini ---
def ringkas_dengan_gemini(text: str, wilayah: str, usaha: str) -> str:
    model = get_rotating_model()
    prompt = (
        f"Buat paragraf ringkas dan padu 2 kalimat dengan maksimal 40 kata. "
        f"Paragraf fokus berkaitan dengan '{usaha}' di '{wilayah}'."
        f"Jika teks TIDAK membahas tentang '{usaha}' di'{wilayah}' dan tidak memuat fenomena ekonomi yang berdampak pada kenaikan atau penurunan {usaha} di {wilayah}, tulis 'TIDAK RELEVAN'.\n\nTeks: {text}"
    )
    try:
        return model.generate_content(prompt).text
    except Exception as e:
        return f"[Ringkasan gagal: {e}]"

# ============================================================
# 3. PEMROSESAN ARTIKEL
# ============================================================

# --- A. Filter Berita Relevan ---
def teks_relevan_dengan_keyword(text: str, LAPANGAN_USAHA: List[str], WILAYAH: List[str]) -> bool:
    text_low = text.lower()
    usaha_match   = [kw for kw in LAPANGAN_USAHA if kw.lower() in text_low]
    wilayah_match = [wil for wil in WILAYAH if wil.lower() in text_low]
    if not usaha_match or not wilayah_match:
        return None
    return {"usaha": usaha_match, "wilayah": wilayah_match}

# --- B. Ekstrak Isi Artikel ---
def ekstrak_teks_artikel(url: str, LAPANGAN_USAHA: List[str], WILAYAH: List[str]) -> Optional[str]:
    try:
        res = requests.get(url, timeout=15)
        if not res or not res.content:
            return None
        text = trafilatura.extract(res.content, output_format="txt")
        if not text:
            return None
        text_clean = "\n".join([
            p for p in text.split("\n")
            if not re.search(r'(Baca juga|Artikel terkait|Editor|Penulis)', p, re.I)])
        matches = teks_relevan_dengan_keyword(text_clean, LAPANGAN_USAHA, WILAYAH)
        if not matches:
            return None
        return {"text": text_clean, "matches": matches}
    except:
        return None

# ============================================================
# 4. ORKESTRASI (untuk UI Streamlit)
# ============================================================

# --- A. CSS untuk tombol download ---
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

# --- B. Fungsi konversi DataFrame ke Excel (bytes) ---
def to_excel(df: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Data")
    return output.getvalue()

# --- C. Fungsi tampilkan tabel hasil scraping (AgGrid + tombol download) ---
def show_aggrid(df: pd.DataFrame):
    # Bersihkan index jika masih ada
    df = df.reset_index(drop=True)
    if "index" in df.columns:
        df = df.drop(columns=["index"])

    # Konfigurasi tampilan AgGrid
    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_pagination(paginationPageSize=10)   # pagination
    gb.configure_side_bar()                          # sidebar filter
    gb.configure_default_column(editable=False, groupable=True)
    gridOptions = gb.build()

    # Layout header dan tombol sejajar
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

    # Render tabel menggunakan AgGrid
    AgGrid(
        df,
        gridOptions=gridOptions,
        theme="light",
        fit_columns_on_grid_load=False,
        suppressRowClickSelection=True
    )

# --- D. Fungsi jalankan scraper (utama) ---
def jalankan_scraper_streamlit(WILAYAH: List[str], LAPANGAN_USAHA: List[str], START_DATE: dt.date, END_DATE: dt.date):
    semua_artikel = []

    # Kombinasi setiap wilayah & lapangan usaha → cari artikel
    for w, u in itertools.product(WILAYAH, LAPANGAN_USAHA):
        semua_artikel.extend(cari_artikel_google_news(f'"{w}"+"{u}"', START_DATE, END_DATE))

    # Jika kosong → tampilkan warning
    if not semua_artikel:
        st.warning("Tidak ada artikel ditemukan.")
        return

    # Ambil URL asli & isi artikel (paralel dengan ThreadPoolExecutor)
    with ThreadPoolExecutor(max_workers=20) as ex:
        urls = list(ex.map(ambil_url_asli, semua_artikel))
        teks = list(ex.map(lambda u: ekstrak_teks_artikel(u, LAPANGAN_USAHA, WILAYAH), urls))

    # Proses hasil scraping
    records = []
    for entry, url, result in zip(semua_artikel, urls, teks):
        if not result: 
            continue

        # Parsing tanggal publikasi
        try:
            pub_dt = datetime.strptime(entry.published, "%a, %d %b %Y %H:%M:%S %Z")
            tanggal = pub_dt.strftime("%d %b %Y")
        except:
            tanggal = entry.published

        # Ringkas teks dengan Gemini, gunakan wilayah & usaha yang match
        ringkasan = ringkas_dengan_gemini(
            result["text"],
            ", ".join(result["matches"]["wilayah"]),
            ", ".join(result["matches"]["usaha"])
        )

        # Hanya simpan artikel relevan
        if "TIDAK RELEVAN" not in ringkasan.strip().upper():
            records.append({
                "Tanggal": tanggal,
                "Judul": entry.title,
                "Sumber": getattr(entry, "source", {}).title if hasattr(entry, "source") else "-",
                "Wilayah": ", ".join(result["matches"]["wilayah"]),
                "Usaha": ", ".join(result["matches"]["usaha"]),
                "Ringkasan": ringkasan,
                "URL": url
            })

    # Simpan hasil ke session_state agar bisa ditampilkan ulang
    st.session_state.scraped_data = pd.DataFrame(records)

    # Info jumlah artikel yang berhasil diproses
    st.success(f"✅ Artikel terproses: {len(records)}")

# ============================================================
# 5. STREAMLIT UI
# ============================================================

# --- A. Input Data Wilayah dan Lapangan Usaha ---
df_usaha = pd.read_csv("https://docs.google.com/spreadsheets/d/1cSISqNtyiGiyZ4nqTrTxBWIO7U98RBS5Z9ehBMWadYo/export?format=csv&gid=233383135")
df_wilayah = pd.read_csv("https://docs.google.com/spreadsheets/d/1cSISqNtyiGiyZ4nqTrTxBWIO7U98RBS5Z9ehBMWadYo/export?format=csv&gid=0")
# Dropdown hanya ambil nama kolom (judul lapangan usaha)
daftar_usaha = df_usaha.columns.tolist()
daftar_wilayah = df_wilayah.columns.tolist()

# --- B. Konfigurasi halaman ---
st.set_page_config(page_title="Scraper Berita PDRB", layout="wide")

# --- C. CSS custom ---
st.markdown(
    """
    <style>
        /* ===== Global ===== */
        .stApp, header[data-testid="stHeader"] { background: #FFF !important; color: #000 !important; border-bottom: 3px solid #e7dfdd; /* garis bawah */
            height: 70px;}}
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
            margin: 0 0 10px 0 !important;
        }

        /* ===== FINAL OVERRIDE (HARUS DI BAWAH) =====*/
        div[data-baseweb="select"] > div {
            color: #000 !important;         /* memastikan teks/value tetap terlihat */
            background: #FFF !important;     /* memastikan latar tetap putih */
            padding: 4px 8px !important;     /* konsisten dengan tinggi kontrol */
            line-height: 1.4 !important;
        }
    </style>
    """,
    unsafe_allow_html=True
)

# --- D. Logo ---
with open("Logo.png", "rb") as f:
    encoded = base64.b64encode(f.read()).decode()

# Sisipkan logo di pojok kiri atas
st.markdown(
    f"""
    <style>
        [data-testid="stHeader"]::before {{
            content: "";
            position: absolute;
            top: 5px; left: 20px;
            height: 250px; width: 250px;
            background-image: url("data:image/png;base64,{encoded}");
            background-size: contain;
            background-repeat: no-repeat;
        }}
    </style>
    """,
    unsafe_allow_html=True
)

# --- E. Judul ---
# Tambah jarak agar judul tidak menabrak logo
st.markdown("<div style='padding-top:10px'></div>", unsafe_allow_html=True)
st.markdown(
    "<h1 class='centered-title'>Web Scraping Berita Ekonomi</h1>",
    unsafe_allow_html=True
)

# --- F. Kotak Input (Wilayah, Lapangan Usaha, Periode) ---
col1, col2, _, col3, _, col4, col5 = st.columns([0.8, 4, 0.2, 4, 0.2, 4, 0.8])
with col2:
    st.markdown("**Wilayah**")
    if st.session_state.get("wilayah_mode", "Opsi") == "Opsi":
        wilayah_input = st.selectbox("Pilih Wilayah", daftar_wilayah, index=0, label_visibility="collapsed")
    else:
        wilayah_input = st.text_input("Masukkan Wilayah Manual", "", label_visibility="collapsed")
    wilayah_mode = st.radio("Metode Input Wilayah", ["Opsi", "Manual"], horizontal=True, key="wilayah_mode", label_visibility="collapsed")
    scrape_button = st.button("🔍 Mulai Scraping", key="scrape_button")

with col3:
    st.markdown("**Lapangan Usaha**")
    if st.session_state.get("usaha_mode", "Opsi") == "Opsi":
        usaha_input = st.selectbox("Pilih Lapangan Usaha", daftar_usaha, index=0, label_visibility="collapsed")
    else:
        usaha_input = st.text_input("Masukkan Usaha Manual", "", label_visibility="collapsed")
    usaha_mode = st.radio("Metode Input Usaha", ["Opsi", "Manual"], horizontal=True, key="usaha_mode", label_visibility="collapsed" )

with col4:
    st.markdown("**Periode Tanggal**")
    periode = st.date_input("",
        label_visibility="collapsed",
        key="Tanggal",
        value=(dt.date(2025, 8, 19), dt.date(2025, 8, 28)),
        format="YYYY-MM-DD"
    )
    if isinstance(periode, tuple) and len(periode) == 2:
        start_date, end_date = periode
    else:
        st.error("⚠️ Harap pilih rentang tanggal.")

# --- G. DataFrame Kosong Awal ---
if "scraped_data" not in st.session_state:
    st.session_state.scraped_data = pd.DataFrame(
        columns=["Tanggal","Judul","Sumber","Wilayah","Usaha","Ringkasan","URL"]
    )

# --- H. Jalankan Scraper ---
if scrape_button:
    if wilayah_input:
        wilayah_key = wilayah_input.strip()
        if wilayah_mode == "Opsi" and wilayah_key in df_wilayah.columns:
            WILAYAH = df_wilayah[wilayah_key].dropna().astype(str).tolist()
        else:
            WILAYAH = [wilayah_input.strip()]
    else:
        WILAYAH = []

    if usaha_input:
        usaha_key = usaha_input.strip()
        if usaha_mode == "Opsi" and usaha_key in df_usaha.columns:
            LAPANGAN_USAHA = df_usaha[usaha_key].dropna().astype(str).tolist()
        else:
            LAPANGAN_USAHA = [usaha_input.strip()]
    else:
        LAPANGAN_USAHA = []
    jalankan_scraper_streamlit(WILAYAH, LAPANGAN_USAHA, start_date, end_date)

# --- I. Tampilkan Data ---
show_aggrid(st.session_state.scraped_data)
