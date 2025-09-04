# Scraper PDRB (Streamlit + Gemini)

Aplikasi ini digunakan untuk scraping berita ekonomi dari Google News berdasarkan wilayah dan lapangan usaha, lalu meringkas isi dengan model Gemini.

## 🚀 Deploy ke Streamlit Cloud

1. Upload repo ini ke GitHub (private/public).
2. Tambahkan secrets di **Streamlit Cloud**:
   - Buka menu **Settings > Secrets**
   - Tambahkan format seperti ini:
     ```toml
     API_KEYS = ["API_KEY_1", "API_KEY_2", "API_KEY_3"]
     ```
3. Deploy aplikasi → pilih `app.py` sebagai entrypoint.

## 📦 Requirement
Semua dependency ada di `requirements.txt`.
