# Streamlit Community Cloud — Dağıtım Rehberi (çok kullanıcı)

Bu proje **multi-user** modda tasarlanmıştır: her ziyaretçi sidebar’dan kendi `GEMINI_API_KEY` değerini girer. Böylece **hosting maliyeti sıfıra yakın** kalır ve **API faturası kullanıcıya** yazılır.

## Neden bu yöntem?

| Yaklaşım | Risk |
|----------|------|
| Herkes yerelde `git clone` | Kurulum bariyeri yüksek |
| Cloud + senin API key’in Secrets’ta | Tüm trafik senin faturana; bot / kötüye kullanım riski |
| Cloud + herkes kendi key’i | Düşük risk; tavsiye edilen |

## Kod tarafında ne yapıyoruz?

`main.py` içinde Streamlit Cloud konteyneri algılanırsa (`/mount/src` varlığı), varsayılan API anahtarı **boş** tutulur; ortam değişkeninden otomatik doldurma **yapılmaz**. Böylece yanlışlıkla deploy sırasında ayarlanmış bir secret tüm kullanıcılara “hazır anahtar” olarak dağıtılmaz.

Yerel geliştirmede davranış aynı kalır: `.env` → `load_dotenv()` ile doldurulabilir.

## Deploy checklist

- [ ] `.env` repoda yok
- [ ] GitHub’da son commit temiz
- [ ] Streamlit Cloud’da Main file: `main.py`
- [ ] Python sürümü: `requirements.txt` yeterli (Cloud otomatik seçer)
- [ ] Arkadaşlara: “Anahtarı [AI Studio](https://aistudio.google.com/apikey)’dan al, sidebar’a yapıştır” de

## Limitler (ücretsiz Cloud)

- Uygulama **uykuya** düşebilir; ilk tıklamada bir süre uyanır.
- Kaynaklar sınırlı; çok ağır eşzamanlı iş yükünde yavaşlayabilir veya zaman aşımı olabilir.
- Uzun süren **Batch** işleri Streamlit oturum zaman aşımına takılabilir — kritik batch’leri yerelde veya daha uzun timeout’lu ortamda çalıştırmayı düşün.

## Sorun giderme

- **“API key gerekli”**: Sidebar’a key girilmemiş veya boşluklu kopyalanmış.
- **429 RESOURCE_EXHAUSTED**: Kota / rate limit; kullanıcı kendi Google projesinde faturalandırma ve limitleri kontrol etmeli.
- **Uygulama uyandıktan sonra state sıfırlandı**: Normal; uzun işlerde ilerlemeyi kaydetmek ileride iyileştirilebilir (şu an oturum bazlı).
