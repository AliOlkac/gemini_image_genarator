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
python -m pip install -r requirements.txt
```

### 5) API anahtarını ayarlayın

En kolayı: uygulamayı açın, sol menüdeki **🔑 API Anahtarı** alanına yapıştırıp **Kaydet**'e basın. Anahtar proje kökündeki **`.env`** dosyasına yazılır; sayfa yenilense veya uygulama yeniden başlasa da silinmez.

İsterseniz `.env` dosyasını elle de oluşturabilirsiniz:

```env
GEMINI_API_KEY=buraya_ai_studio_anahtariniz
```

- Bu dosyayı **Git’e eklemeyin**; repoda `.env` yok sayılır.
- Anahtarı **asla** public repoda, ekran görüntüsünde veya `.env.example` içinde paylaşmayın.

### 6) Uygulamayı çalıştırın

```powershell
# Sanal ortam açıkken (.venv)
python -m streamlit run main.py
```

> Not: Bazı Windows kurulumlarında **Uygulama Denetimi (App Control)** ilkesi `.venv\Scripts` içindeki `pip.exe` / `streamlit.exe` kısayollarını engeller. Bu yüzden komutlar `python -m ...` biçiminde yazıldı; `start.bat` de aynı yolu kullanır.

Tarayıcıda genelde `http://localhost:8501` açılır. Windows'ta `start.bat` dosyasına çift tıklamak da kurulumu kontrol edip uygulamayı başlatır.

---

## Nasıl çalışır?

- **Kullanıcı seçimi:** Açılışta isminizi seçin ya da yeni kullanıcı oluşturun. Her kullanıcının formu, referans görseli ve üretimleri ayrı tutulur; aynı anda birden fazla kişi kullanabilir, görseller karışmaz.
- **Arka planda üretim:** "Üretimi Başlat"a bastıktan sonra iş sunucuda arka planda çalışır. Sayfayı yenilemek, başka bir şeye basmak veya sekmeyi kapatmak işi durdurmaz; geri döndüğünüzde kaldığı yerden görürsünüz.
- **Batch işleri:** Google'a gönderildiği anda kaydedilir ve 20 saniyede bir kontrol edilir. Bitince görseller **otomatik indirilir** (tarayıcı kapalı olsa bile, uygulama çalıştığı sürece). Uygulama kapanıp açılırsa takip kaldığı yerden sürer. İstediğiniz an **İptal** edebilirsiniz.
- **Kurtarma:** Eski sürümde sayfa kapandığı için sonucu indirilmemiş batch işleri için sol menüdeki **🔎 Hesaptaki batch işlerini tara** butonunu kullanın.
- **Başarısızlar:** Her varyasyonun hata sebebi listede görünür; "Tamamlanmayanları forma aktar" ile tekrar deneyebilirsiniz.

---

## Klasörler

| Yol | Açıklama |
|-----|----------|
| `outputs/<kullanıcı>/<iş>/` | Üretilen görseller; her iş ayrı klasörde, dosya adları varyasyondan (`001_kirmizi-arka-plan.png`) |
| `data/` | Kullanıcı formları ve iş kayıtları (yerel; commit edilmez) |
| `.venv/` | Sanal ortam (yerel; commit edilmez) |

---

## Sorun giderme

| Sorun | Ne yapmalı |
|-------|------------|
| `streamlit` tanınmıyor | `Activate.ps1` ile venv açık mı kontrol edin; `python -m pip install -r requirements.txt` tekrar. |
| `Uygulama Denetimi ilkesi bu dosyayı engelledi` | Windows, venv içindeki `pip.exe` / `streamlit.exe` kısayollarını engelliyor. Komutları `.\.venv\Scripts\python.exe -m pip ...` ve `.\.venv\Scripts\python.exe -m streamlit run main.py` şeklinde çalıştırın. |
| `429 RESOURCE_EXHAUSTED` | Ücretsiz kota / dakikalık limit; AI Studio’da plan ve limitlere bakın; eşzamanlı istek sayısını düşürün. |
| API key hatası | `.env` dosyası proje kökünde mi, değişken adı tam `GEMINI_API_KEY` mi; Streamlit’i yeniden başlatın. |

---

## Lisans / katkı

Proje sahibinin tercihine göre lisans ekleyebilirsiniz.
