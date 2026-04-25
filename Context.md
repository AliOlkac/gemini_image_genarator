# Proje: Gemini 2.5 Flash Image Batcher (Local)

## 🎯 Hedef
Kullanıcının yerel makinesinde çalışan, Gemini 2.5 Flash Image modelini kullanarak "Batch API" üzerinden toplu görsel üreten bir araç yapmak.

## 🛠️ Teknik Gereksinimler
- **Model:** `gemini-2.5-flash-image`
- **Yöntem:** Batch API (Maliyet avantajı için .jsonl tabanlı asenkron süreç).
- **Dil:** Python (Backend), Streamlit veya Flask (Arayüz).
- **Girdi:** 1 Referans Görsel (Master Image) + 1 Sabit Metin (Master Prompt) + Çoklu Satır (Varyasyonlar).
- **Çıktı:** Üretilen görsellerin yerel `/outputs` klasörüne paralel indirilmesi.

## 💰 Maliyet Bilgisi (Referans)
- Çıkış Jetonu: Görsel başına 1.290 jeton.
- Fiyat: 1M jeton = 0.30$ (Standart), Batch API ile %50 indirimli.
- Referans Görsel: Giriş maliyetine dahil (yaklaşık 768 jeton).

## 🔄 İş Akışı Kuralları
1. Kullanıcı API Key'i `.env` dosyasından veya kod içinden manuel tanımlar.
2. Master Prompt ve her bir varyasyon satırı birleştirilir.
3. Google SDK kullanılarak bir `.jsonl` dosyası oluşturulur ve `BatchJob` başlatılır.
4. Program, `JobStatus` "COMPLETED" olana kadar belirli aralıklarla sorgulama yapar.
5. Tamamlandığında tüm görsel URL'leri `aiohttp` ile asenkron (paralel) olarak indirilir.