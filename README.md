# Gemini Image Generator (Streamlit)

`gemini-2.5-flash-image` ile toplu görsel üretimi: **Standart API** (anında) veya **Batch API** (daha ucuz, gecikmeli). Arayüz **Streamlit**.

---

## Gereksinimler

- **Python 3.10 veya üzeri** (3.11 / 3.12 önerilir; kurulumda `python --version` ile kontrol edin).
- **Git** (repo klonlamak için, isteğe bağlı).
- **Gemini API anahtarı**: [Google AI Studio](https://aistudio.google.com/apikey) üzerinden oluşturulur. Görsel modeli için genelde **ücretli plan / kredi** gerekir; ücretsiz kotada sık **429** görülebilir.

---

## Bilgisayara kurulum (Windows / PowerShell)

Aşağıdaki komutları **PowerShell** içinde, proje klasöründe çalıştırın.

### 1) Projeyi indirin

```powershell
cd $HOME\Documents\GitHub
git clone https://github.com/KULLANICI_ADIN/gemini_image_genarator.git
cd gemini_image_genarator
```

*(ZIP indirdiyseniz klasörü açıp içine `cd` yapmanız yeterli.)*

### 2) Sanal ortam (venv) oluşturun

Sanal ortam, paketleri sistem Python’undan ayırır; başka projelerle çakışmayı önler.

```powershell
# Proje kökünde olduğunuzdan emin olun
python -m venv .venv
```

- `.venv` klasörü proje içinde oluşur (`.gitignore` ile repoya eklenmez).
- `python` komutu bulunamazsa `py -3.12 -m venv .venv` deneyin (Python Launcher).

### 3) Sanal ortamı etkinleştirin

**PowerShell** (her yeni terminal oturumunda tekrar gerekir):

```powershell
.\.venv\Scripts\Activate.ps1
```

İlk kez “running scripts is disabled” hatası alırsanız (yönetici olmadan genelde geçici çözüm):

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Sonra tekrar `Activate.ps1` çalıştırın.

**CMD** kullanıyorsanız:

```cmd
.venv\Scripts\activate.bat
```

Prompt’un başında `(.venv)` görünmeli.

### 4) Bağımlılıkları yükleyin

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 5) API anahtarını ayarlayın

Proje kökünde **`.env`** dosyası oluşturun (Not Defteri yeterli):

```env
GEMINI_API_KEY=buraya_ai_studio_anahtariniz
```

- Bu dosyayı **Git’e eklemeyin**; repoda `.env` yok sayılır.
- Anahtarı **asla** public repoda, ekran görüntüsünde veya `.env.example` içinde paylaşmayın.

### 6) Uygulamayı çalıştırın

```powershell
# Sanal ortam açıkken (.venv)
streamlit run main.py
```

Tarayıcıda genelde `http://localhost:8501` açılır.

---

## Klasörler

| Yol | Açıklama |
|-----|----------|
| `outputs/` | Üretilen görseller (varsayılan çıktı; `.gitignore` ile repoda takip edilmez) |
| `.venv/` | Sanal ortam (yerel; commit edilmez) |

---

## Sorun giderme

| Sorun | Ne yapmalı |
|-------|------------|
| `streamlit` tanınmıyor | `Activate.ps1` ile venv açık mı kontrol edin; `pip install -r requirements.txt` tekrar. |
| `429 RESOURCE_EXHAUSTED` | Ücretsiz kota / dakikalık limit; AI Studio’da plan ve limitlere bakın; eşzamanlı istek sayısını düşürün. |
| API key hatası | `.env` dosyası proje kökünde mi, değişken adı tam `GEMINI_API_KEY` mi; Streamlit’i yeniden başlatın. |

---

## Lisans / katkı

Proje sahibinin tercihine göre lisans ekleyebilirsiniz.
