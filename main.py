"""
main.py
=======
Gemini 2.5 Flash Image Batcher - Streamlit Arayüzü.

MİMARİ (v3):
    - Arayüz formu gösterir ve iş kaydı oluşturur; üretimi job_manager ARKA PLANDA
      yürütür. Sayfayı yenilemek, başka bir şeye basmak ya da sekmeyi kapatmak
      çalışan işi etkilemez.
    - Her kullanıcı bir profil seçer (adres çubuğunda ?u=isim). Form alanları,
      referans görsel ve üretimler profile özel olarak diskte saklanır; iki kişi
      aynı anda kullansa da görseller karışmaz.
    - API anahtarı .env dosyasına kaydedilir; yenileme / yeniden başlatma sonrası
      da durur.
    - Batch job'lar tarayıcı kapalı olsa bile takip edilir, bitince otomatik iner.

İKİ MOD DESTEĞİ:
    1. Standart API (anında)
    2. Batch API (%50 indirimli, dakikalar - 24 saat)

ÇALIŞTIRMA:
    streamlit run main.py
"""

from __future__ import annotations

import functools
import hashlib
import io
import time
import zipfile
from datetime import datetime
from pathlib import Path

import streamlit as st

import storage
from app_config import (
    DEFAULT_OUTPUT_DIR,
    MODEL_NAME,
    PRICE_PER_IMAGE_BATCH,
    PRICE_PER_IMAGE_STANDARD,
    get_api_key,
    key_fingerprint,
    mask_key,
    save_api_key,
)
from job_manager import HOSTNAME, PROCESS_TOKEN, get_manager


# ---------------------------------------------------------------------------
# Sayfa konfigürasyonu - tüm st.* çağrılarından ÖNCE gelmeli (Streamlit kuralı)
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Gemini 2.5 Flash Image Batcher",
    page_icon="🎨",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Arka plan işleri + batch takipçisi. Süreç başına bir kez oluşur; tüm
# kullanıcılar ve sayfa yenilemeleri aynı yöneticiyi paylaşır.
manager = get_manager()

MODE_LABELS = {"standard": "Standart", "batch": "Batch"}
STATUS_LABELS = {
    "queued": "⏳ Sırada",
    "running": "⚡ Üretiliyor",
    "submitting": "📤 Google'a gönderiliyor",
    "waiting": "🕒 Google'da işleniyor",
    "downloading": "📥 Sonuçlar indiriliyor",
    "done": "✅ Tamamlandı",
    "partial": "🟡 Kısmen tamamlandı",
    "failed": "❌ Başarısız",
    "cancelled": "⏹️ İptal edildi",
    "interrupted": "⚠️ Yarıda kaldı",
}
ITEM_STATUS_LABELS = {
    "ok": "✅ üretildi",
    "failed": "❌ başarısız",
    "cancelled": "⏹️ iptal",
    "pending": "⏳ beklemede",
}
BATCH_STATE_LABELS = {
    "JOB_STATE_QUEUED": "kuyrukta bekliyor",
    "JOB_STATE_PENDING": "kuyrukta bekliyor",
    "JOB_STATE_RUNNING": "görseller üretiliyor",
    "JOB_STATE_SUCCEEDED": "tamamlandı",
    "JOB_STATE_CANCELLING": "iptal ediliyor",
}
FORM_DEFAULTS = {
    "master_prompt": "",
    "variations": "",
    "api_mode": "standart",
    "max_workers": 3,
    "use_auto_prefix": True,
    "output_dir": DEFAULT_OUTPUT_DIR,
}
# Aynı iş bu süre içinde tekrar başlatılırsa çift tıklama sayılır.
DUPLICATE_WINDOW_SECONDS = 10
# Devam eden işler paneli kaç saniyede bir yenilensin.
LIVE_REFRESH_SECONDS = 3


# ===========================================================================
#                              YARDIMCI FONKSİYONLAR
# ===========================================================================
def _fmt_time(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else "?"


def _fmt_duration(seconds: float) -> str:
    minutes, secs = divmod(int(max(seconds, 0)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} sa {minutes} dk" if hours else f"{minutes} dk {secs} sn"


def _fmt_ago(ts: float | None) -> str:
    return f"{int(time.time() - ts)} sn önce" if ts else "henüz yok"


def _fmt_size(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / 1024 / 1024:.1f} MB".replace(".", ",")
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes} B"


def _status_icon(status: str) -> str:
    return STATUS_LABELS.get(status, "•").split(" ")[0]


def _validate_inputs(has_master_image: bool, prompt: str, variations: list[str]) -> list[str]:
    """Form girdilerini doğrular, hata listesi döner."""
    errors: list[str] = []
    if not has_master_image:
        errors.append("Master görsel yüklenmemiş.")
    if not prompt.strip():
        errors.append("Master prompt boş olamaz.")
    if not variations:
        errors.append("En az bir varyasyon satırı gerekli.")
    return errors


def _zip_builder(paths: list[Path]):
    """İndirme butonuna verilecek, TIKLANINCA çalışan ZIP üretici (her rerun'da değil)."""
    def build() -> bytes:
        buffer = io.BytesIO()
        # Görseller zaten sıkıştırılmış; ZIP_STORED hızlı ve boyut farkı yok denecek kadar az.
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:
            for path in paths:
                if path.exists():
                    zf.write(path, arcname=path.name)
        return buffer.getvalue()

    return build


def _load_form_into_session(profile: dict) -> None:
    """
    Profilin kayıtlı form değerlerini widget'lara yükler (yenileme sonrası geri gelir).

    Sadece profil değişince değil, widget değeri oturumda YOKSA da diskten
    yükler: Streamlit, st.rerun() ile yarıda kesilen bir çalıştırmada çizilmemiş
    widget'ların değerini siler. Bu kontrol olmasa form varsayılanlara döner ve
    boş değerler profile yazılırdı.
    """
    st.session_state.setdefault("_uploader_nonce", 0)
    switched = st.session_state.get("_loaded_profile") != profile["slug"]
    form = {**FORM_DEFAULTS, **(profile.get("form") or {})}
    values = {
        "widget_master_prompt": str(form["master_prompt"]),
        "widget_variations": str(form["variations"]),
        "widget_api_mode": form["api_mode"] if form["api_mode"] in ("standart", "batch") else "standart",
        "widget_max_workers": int(min(5, max(1, int(form["max_workers"])))),
        "widget_auto_prefix": bool(form["use_auto_prefix"]),
        "widget_output_dir": str(form["output_dir"] or DEFAULT_OUTPUT_DIR),
    }
    for widget_key, value in values.items():
        if switched or widget_key not in st.session_state:
            st.session_state[widget_key] = value
    st.session_state["_loaded_profile"] = profile["slug"]


def _set_variations(text: str) -> None:
    """Buton callback'i: widget oluşturulmadan önce çalıştığı için değeri değiştirebilir."""
    st.session_state["widget_variations"] = text


def _load_run_into_form(run: dict) -> None:
    """Eski bir işin prompt'unu, varyasyonlarını ve referans görselini forma geri yükler."""
    st.session_state["widget_master_prompt"] = run.get("master_prompt") or ""
    variations = [i["variation"] for i in run.get("items", []) if i.get("variation")]
    if variations:
        st.session_state["widget_variations"] = "\n".join(variations)
    st.session_state["widget_auto_prefix"] = bool(run.get("use_auto_prefix", True))

    # Referans görselin kopyası iş klasöründe duruyor; profildeki görseli onunla değiştir.
    master_file = run.get("master_file")
    if master_file:
        master_path = Path(run["output_dir"]) / master_file
        if master_path.exists():
            storage.save_profile_master(
                run["owner"], master_path.read_bytes(), f"referans{master_path.suffix}"
            )
            st.session_state["_uploader_nonce"] = st.session_state.get("_uploader_nonce", 0) + 1
    st.session_state["_form_loaded_from_run"] = True


def _toggle_select_all(run_ids: tuple[str, ...]) -> None:
    """'Tümünü seç' kutusu: callback script'ten önce çalıştığı için kutuları değiştirebilir."""
    value = bool(st.session_state.get("select_all_runs"))
    for run_id in run_ids:
        st.session_state[f"select_{run_id}"] = value


def _set_flag(key: str, value: bool) -> None:
    """Onay adımlarını açıp kapatır. Callback olarak kullanılır: script'ten önce
    çalıştığı için st.rerun() gerekmez (rerun, henüz çizilmemiş seçim kutularının
    durumunu silerdi)."""
    if value:
        st.session_state[key] = True
    else:
        st.session_state.pop(key, None)


def _delete_runs(run_ids: list[str]) -> None:
    """Seçilen işleri kayıt + klasör olarak siler, seçim kutularını temizler."""
    deleted = 0
    for run_id in run_ids:
        if storage.delete_run(run_id):
            deleted += 1
        st.session_state.pop(f"select_{run_id}", None)
        st.session_state.pop(f"confirm_delete_{run_id}", None)
    st.session_state["select_all_runs"] = False
    st.session_state.pop("_confirm_bulk_delete", None)
    st.session_state["_delete_notice"] = (
        f"{deleted} üretim ve klasörü silindi." if deleted else "Silinecek kayıt bulunamadı."
    )


def _remove_master_callback(slug: str) -> None:
    storage.clear_profile_master(slug)
    # Uploader'ın key'i değişir → içindeki eski dosya da temizlenir.
    st.session_state["_uploader_nonce"] = st.session_state.get("_uploader_nonce", 0) + 1


# ===========================================================================
#                              PROFİL SEÇİMİ
# ===========================================================================
def _render_profile_picker() -> None:
    st.title("🎨 Gemini 2.5 Flash Image Batcher")
    st.subheader("👤 Kim kullanıyor?")
    st.caption(
        "Her kullanıcının formu, referans görseli ve üretimleri ayrı tutulur. "
        "Aynı anda birden fazla kişi kullanabilir; görseller birbirine karışmaz."
    )

    profiles = storage.list_profiles()
    if profiles:
        cols = st.columns(min(len(profiles), 4))
        for i, profile in enumerate(profiles):
            with cols[i % len(cols)]:
                if st.button(profile["name"], key=f"pick_{profile['slug']}", width="stretch"):
                    st.query_params["u"] = profile["slug"]
                    st.rerun()

    with st.form("new_profile", clear_on_submit=True):
        name = st.text_input("Yeni kullanıcı", placeholder="Örn: Ali")
        if st.form_submit_button("Başla", type="primary"):
            try:
                profile = storage.create_profile(name)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.query_params["u"] = profile["slug"]
                st.rerun()


profile_slug = st.query_params.get("u")
profile = storage.get_profile(profile_slug) if profile_slug else None
if profile is None:
    _render_profile_picker()
    st.stop()

_load_form_into_session(profile)
api_key = get_api_key()

if st.session_state.pop("_form_loaded_from_run", False):
    st.toast("Eski işin prompt'u, varyasyonları ve referans görseli forma yüklendi.", icon="↩️")


# ===========================================================================
#                                  SIDEBAR
# ===========================================================================
def _save_api_key_callback() -> None:
    # Callback script'ten ÖNCE çalışır: st.rerun() gerekmez, form yarıda kesilmez.
    try:
        save_api_key(st.session_state.get("widget_new_api_key", ""))
    except ValueError as exc:
        st.session_state["_api_key_message"] = ("error", str(exc))
    else:
        st.session_state["_api_key_message"] = ("saved", "API anahtarı kaydedildi.")


def _api_key_form(button_label: str) -> None:
    # clear_on_submit: anahtar kaydedildikten sonra kutuda/oturumda kalmaz.
    with st.form("api_key_form", clear_on_submit=True, border=False):
        st.text_input(
            "Gemini API Key",
            type="password",
            key="widget_new_api_key",
            help=(
                "Anahtarını https://aistudio.google.com/apikey adresinden al. "
                "Proje klasöründeki .env dosyasına kaydedilir; sayfa yenilense "
                "ya da uygulama yeniden başlasa da silinmez."
            ),
        )
        st.form_submit_button(button_label, on_click=_save_api_key_callback)


with st.sidebar:
    st.title("⚙️ Ayarlar")

    user_col, switch_col = st.columns([3, 2], vertical_alignment="center")
    user_col.markdown(f"👤 **{profile['name']}**")
    if switch_col.button("Değiştir", key="switch_profile", width="stretch"):
        st.query_params.clear()
        st.session_state.pop("_loaded_profile", None)
        st.rerun()

    st.subheader("🔑 API Anahtarı")
    api_key_message = st.session_state.pop("_api_key_message", None)
    if api_key_message and api_key_message[0] == "error":
        st.error(api_key_message[1])
    elif api_key_message:
        st.toast(api_key_message[1], icon="🔑")
    if api_key:
        st.success(f"Kayıtlı anahtar: `{mask_key(api_key)}`")
        with st.expander("Anahtarı değiştir"):
            _api_key_form("Yeni anahtarı kaydet")
    else:
        st.warning("API anahtarı kayıtlı değil. Üretim için önce kaydet.")
        _api_key_form("Kaydet")

    output_dir = st.text_input(
        "Çıktı klasörü",
        key="widget_output_dir",
        help="Görseller <klasör>/<kullanıcı>/<iş>/ altına, her iş ayrı klasöre kaydedilir.",
    )

    st.divider()
    st.subheader("🎯 Üretim Modu")

    api_mode = st.radio(
        label="Hangi API ile üretim yapılacak?",
        options=["standart", "batch"],
        format_func=lambda x: {
            "standart": "Standart API (anında)",
            "batch": "Batch API (%50 ucuz, yavaş)",
        }[x],
        key="widget_api_mode",
        help=(
            "Standart: Anlık üretim. "
            "Batch: %50 ucuz ama 24 saate kadar sürebilir; sayfayı kapatsan da "
            "arka planda takip edilir ve bitince otomatik indirilir."
        ),
    )

    # Batch'te geçerli değil çünkü Google sunucusu zaten paralelize ediyor.
    # Batch modunda da çiziliyor (disabled): çizilmeyen widget'ın değeri Streamlit
    # tarafından siliniyor, standarda dönünce ayar kaybolmasın.
    max_workers = st.slider(
        "Eş zamanlı istek sayısı",
        min_value=1,
        max_value=5,
        key="widget_max_workers",
        disabled=api_mode == "batch",
        help=(
            "Daha yüksek = daha hızlı ama 429 (rate limit) riski. "
            "Paid tier için 3 dengeli. Batch modunda kullanılmaz."
        ),
    )

    # Hem Standart hem Batch'te aynı modeli çağırıyoruz, dolayısıyla
    # "STOP-without-image" sorunu ikisinde de var; çözüm de aynı: imperatif önek.
    use_auto_prefix = st.checkbox(
        "🎯 Otomatik 'görsel üret' öneki ekle",
        key="widget_auto_prefix",
        help=(
            "Master prompt'unun başına şu cümle eklenir:\n\n"
            "\"Based on the provided reference image, generate a new "
            "image that matches the description below.\"\n\n"
            "Modelin sadece-text döndürme eğilimini ~%80 azaltır. "
            "Maliyeti yok denecek kadar az (~25 token = ~$0.000003). "
            "Hem Standart hem Batch modunda çalışır. "
            "Master prompt'una zaten benzer bir komut yazdıysan kapatabilirsin."
        ),
    )

    st.divider()

    # FİYAT NOTU: Her görsel = 1290 output token × $30/1M = $0.039 (Standard).
    # Batch %50 indirimle $0.0195/görsel.
    if api_mode == "standart":
        st.success(
            "💡 **Standart API**\n\n"
            "- Anında sonuç (saniyeler)\n"
            "- Arka planda çalışır; sayfayı yenilemek işi durdurmaz\n"
            f"- **~${PRICE_PER_IMAGE_STANDARD}/görsel** (tam fiyat)"
        )
    else:
        st.warning(
            "⚠️ **Batch API**\n\n"
            f"- **~${PRICE_PER_IMAGE_BATCH}/görsel** (%50 indirim)\n"
            "- Dakikalar - 24 saat arası sürebilir\n"
            "- Sayfayı kapatabilirsin; bitince otomatik indirilir"
        )

    st.divider()
    st.subheader("🛟 Kurtarma")
    import_clicked = st.button(
        "🔎 Hesaptaki batch işlerini tara",
        width="stretch",
        disabled=not api_key,
        help=(
            "Google hesabındaki, bu listede olmayan görsel batch job'larını bulur ve "
            "senin işlerine ekler. Eskiden sayfa kapandığı için sonucu indirilmemiş "
            "job'lar bitmişse otomatik indirilir."
        ),
    )
    if import_clicked:
        with st.spinner("Google hesabındaki batch işleri taranıyor..."):
            try:
                counts = manager.import_account_batches(
                    owner=profile["slug"], output_base=output_dir
                )
            except Exception as exc:
                st.error(f"Tarama başarısız: {exc}")
            else:
                found = counts["imported"] + counts["relinked"]
                if found:
                    st.success(
                        f"{found} batch işi listeye eklendi. Bitmiş olanlar birkaç "
                        "saniye içinde indirilecek."
                    )
                else:
                    st.info("Takip edilmeyen yeni bir görsel batch işi bulunamadı.")

    st.caption(f"Model: `{MODEL_NAME}`")
    st.caption("SDK: `google-genai`")


# ===========================================================================
#                                ANA SAYFA
# ===========================================================================
st.title("🎨 Gemini 2.5 Flash Image Batcher")
st.markdown(
    "Master görsel + Master prompt + Varyasyonlar ile toplu üretim. "
    "İşler arka planda çalışır; sayfayı yenilesen de kaldığı yerden görürsün."
)

col_left, col_right = st.columns([1, 1])

with col_left:
    st.subheader("📤 1. Master Görsel")
    # Nonce: "Görseli kaldır" sonrası uploader'ı da boşaltmak için key değişir.
    uploaded_image = st.file_uploader(
        "Referans görseli yükle",
        type=["png", "jpg", "jpeg", "webp"],
        help="Tüm varyasyonlar bu görseli temel alacak. Yüklenen görsel profiline kaydedilir.",
        key=f"widget_master_file_{st.session_state['_uploader_nonce']}",
    )
    if uploaded_image is not None:
        uploaded_bytes = uploaded_image.getvalue()
        if (profile.get("master") or {}).get("sha1") != hashlib.sha1(uploaded_bytes).hexdigest():
            profile = storage.save_profile_master(
                profile["slug"], uploaded_bytes, uploaded_image.name
            ) or profile

    master_path = storage.profile_master_path(profile)
    if master_path is not None:
        st.image(
            str(master_path),
            caption=f"Master Görsel: {profile['master']['name']}",
            width="stretch",
        )
        if uploaded_image is None:
            st.caption("📌 Kayıtlı görsel kullanılıyor (sayfa yenilense de durur).")
        st.button(
            "🗑️ Görseli kaldır",
            key="remove_master",
            on_click=_remove_master_callback,
            args=(profile["slug"],),
        )

with col_right:
    st.subheader("✍️ 2. Master Prompt")
    master_prompt = st.text_area(
        "Sabit metin (her varyasyona uygulanacak temel komut)",
        height=140,
        placeholder=(
            "Örnek: Bu görselin stilini ve karakterlerini koruyarak "
            "aşağıdaki varyasyona uygun yeni bir versiyon üret."
        ),
        help=(
            "💡 Otomatik önek varsayılan olarak AÇIK (sidebar'dan görebilirsin). "
            "Yani senin yazdığın prompt'un başına model'i 'görsel üret' "
            "demeye zorlayan İngilizce kısa bir cümle ekleniyor. "
            "Sen sadece sahnenin/varyasyonun ne olacağını anlat."
        ),
        key="widget_master_prompt",
    )

    # Auto-prefix kapalıysa ve kullanıcı imperatif yazmadıysa uyar.
    if not use_auto_prefix and master_prompt.strip():
        _prompt_hint_keywords = ["üret", "generate", "create", "draw", "produce", "make"]
        if not any(kw in master_prompt.lower() for kw in _prompt_hint_keywords):
            st.caption(
                "⚠️ Otomatik önek kapalı ve prompt'unda 'görsel üret' "
                "benzeri bir komut göremedim. Başarı oranı düşebilir - "
                "ya sidebar'dan öneki aç ya da prompt'una imperatif komut ekle."
            )

    st.subheader("🔀 3. Varyasyonlar")
    variations_text = st.text_area(
        "Her satıra bir varyasyon yaz",
        height=200,
        placeholder=(
            "Kırmızı arka plan\n"
            "Mavi arka plan\n"
            "Gece sahnesi, neon ışıklar\n"
            "Ormanlık alan, yağmurlu hava"
        ),
        key="widget_variations",
    )
    variations_list = [line.strip() for line in variations_text.splitlines() if line.strip()]

    # --- CANLI MALİYET TAHMİNİ ---
    if variations_list:
        _variation_count = len(variations_list)
        _cost_standard = _variation_count * PRICE_PER_IMAGE_STANDARD
        _cost_batch = _variation_count * PRICE_PER_IMAGE_BATCH
        _try_rate = 36  # Yaklaşık USD/TRY - değişebilir

        cost_col1, cost_col2 = st.columns(2)
        with cost_col1:
            st.metric(
                f"💸 Standart ({_variation_count} görsel)",
                f"${_cost_standard:.2f}",
                f"~{_cost_standard * _try_rate:.0f} TL",
                delta_color="off",
            )
        with cost_col2:
            st.metric(
                f"🟢 Batch ({_variation_count} görsel)",
                f"${_cost_batch:.2f}",
                f"~{_cost_batch * _try_rate:.0f} TL (-50%)",
                delta_color="normal",
            )
        # \$: markdown iki $ arasını LaTeX formülü sanıp metni bozuyordu.
        st.caption(
            f"📊 Hesap: {_variation_count} görsel × \\${PRICE_PER_IMAGE_STANDARD} (Standard) "
            f"veya × \\${PRICE_PER_IMAGE_BATCH} (Batch). Kur yaklaşık {_try_rate} TL/USD varsayımı. "
            "Input token maliyeti dahil değil (yaklaşık \\$0.0002/görsel - negligible)."
        )

# Form değerlerini profile yaz: sayfa yenilense ya da uygulama kapansa da geri gelir.
_form_values = {
    "master_prompt": master_prompt,
    "variations": variations_text,
    "api_mode": api_mode,
    "max_workers": int(max_workers),
    "use_auto_prefix": bool(use_auto_prefix),
    "output_dir": output_dir,
}
if (profile.get("form") or {}) != _form_values:
    storage.update_profile(profile["slug"], lambda p: p.update(form=_form_values))

st.divider()


# ---------------------------------------------------------------------------
# Üretimi Başlat
# ---------------------------------------------------------------------------
run_mode = "standard" if api_mode == "standart" else "batch"
start_button = st.button(
    f"🚀 Üretimi Başlat ({MODE_LABELS[run_mode]} Mod)",
    type="primary",
    width="stretch",
    disabled=not api_key,
)
if not api_key:
    st.caption("🔑 Başlatmak için önce sidebar'dan API anahtarını kaydet.")

if start_button:
    validation_errors = _validate_inputs(master_path is not None, master_prompt, variations_list)
    if validation_errors:
        for err in validation_errors:
            st.error(f"❌ {err}")
    else:
        fingerprint = hashlib.sha1(
            "\x1f".join([
                run_mode,
                master_prompt.strip(),
                "\n".join(variations_list),
                str(use_auto_prefix),
                profile["master"]["sha1"],
            ]).encode("utf-8")
        ).hexdigest()
        # Çift tıklama koruması sunucu tarafında: aynı iş birkaç saniye içinde
        # ikinci kez oluşturulmaz (buton durumu tarayıcıda gecikmeli güncellenir).
        duplicate = any(
            r.get("fingerprint") == fingerprint
            and r.get("status") != "failed"
            and time.time() - r.get("created_at", 0) < DUPLICATE_WINDOW_SECONDS
            for r in storage.list_runs(owner=profile["slug"])
        )
        if duplicate:
            st.warning("Bu iş az önce başlatıldı; aşağıdaki 'Devam eden işler' bölümünden takip edebilirsin.")
        else:
            new_run = storage.create_run(
                owner=profile["slug"],
                mode=run_mode,
                master_prompt=master_prompt.strip(),
                variations=variations_list,
                use_auto_prefix=bool(use_auto_prefix),
                max_workers=int(max_workers),
                output_base=output_dir,
                master_bytes=master_path.read_bytes(),
                master_name=profile["master"]["name"],
                key_fp=key_fingerprint(api_key),
                fingerprint=fingerprint,
                host=HOSTNAME,
                process_token=PROCESS_TOKEN,
            )
            manager.start_run(new_run, api_key)
            st.toast(
                f"{MODE_LABELS[run_mode]} iş başlatıldı ({len(variations_list)} varyasyon).",
                icon="🚀",
            )
            st.rerun()


# ===========================================================================
#                         DEVAM EDEN İŞLER (canlı panel)
# ===========================================================================
def _render_cancel_control(run: dict) -> None:
    if run.get("cancel_requested"):
        st.caption("İptal ediliyor…")
        return
    confirm_key = f"confirm_cancel_{run['id']}"
    if not st.session_state.get(confirm_key):
        if st.button("⏹️ İptal", key=f"cancel_{run['id']}", width="stretch"):
            st.session_state[confirm_key] = True
            st.rerun(scope="fragment")
        return
    st.caption("Emin misin?")
    yes_col, no_col = st.columns(2)
    if yes_col.button("Evet", key=f"cancel_yes_{run['id']}", type="primary", width="stretch"):
        st.session_state.pop(confirm_key, None)
        try:
            manager.cancel_run(run["id"])
        except Exception as exc:
            st.error(f"İptal edilemedi: {exc}")
        else:
            st.toast("İptal isteği gönderildi.", icon="⏹️")
            st.rerun(scope="fragment")
    if no_col.button("Hayır", key=f"cancel_no_{run['id']}", width="stretch"):
        st.session_state.pop(confirm_key, None)
        st.rerun(scope="fragment")


def _render_active_run(run: dict) -> None:
    items = run["items"]
    total = len(items)
    ok_count = sum(1 for i in items if i["status"] == "ok")
    failed = [i for i in items if i["status"] == "failed"]
    finished_count = sum(1 for i in items if i["status"] != "pending")

    with st.container(border=True):
        info_col, action_col = st.columns([5, 1])
        info_col.markdown(
            f"**{storage.run_label(run)}** · {MODE_LABELS[run['mode']]} · {_fmt_time(run['created_at'])} · "
            f"{total} varyasyon — {STATUS_LABELS.get(run['status'], run['status'])}"
        )
        with action_col:
            _render_cancel_control(run)

        if run["mode"] == "standard":
            st.progress(
                finished_count / total if total else 0.0,
                text=f"{finished_count}/{total} tamamlandı · ✅ {ok_count} · ❌ {len(failed)}",
            )
            for item in failed[-3:]:
                st.caption(f"❌ {item['key']} {item['variation'][:40]}: {item['error']}")
            return

        batch = run.get("batch") or {}
        if run["status"] in ("queued", "submitting") or not batch.get("job_name"):
            st.info("📤 Referans görsel ve istekler Google'a yükleniyor…")
            return
        elapsed = time.time() - (batch.get("submitted_at") or run["created_at"])
        state = batch.get("state") or ""
        st.info(
            f"🕒 Google'da: **{BATCH_STATE_LABELS.get(state, state or '?')}** · "
            f"gönderileli {_fmt_duration(elapsed)} · son kontrol {_fmt_ago(batch.get('last_checked_at'))}"
        )
        st.caption(
            "Batch işleri genelde dakikalar-saatler sürer (en fazla 24 saat). "
            "Sayfayı kapatabilirsin: sonuç hazır olunca otomatik indirilir ve "
            "aşağıdaki listede görünür."
        )
        if batch.get("last_error"):
            st.warning(batch["last_error"])


@st.fragment(run_every=LIVE_REFRESH_SECONDS)
def _live_runs_panel(owner: str) -> None:
    active = [
        r for r in storage.list_runs(owner=owner)
        if r["status"] in storage.ACTIVE_STATUSES
    ]
    if not active:
        # Son aktif iş de bitti: tüm sayfayı yenile ki sonuç aşağıda görünsün
        # ve bu periyodik yenileme dursun.
        st.rerun()
    st.subheader(f"⏳ Devam eden işler ({len(active)})")
    st.caption(
        "İşler arka planda çalışır: sayfayı yenileyebilir, başka şeylere basabilir "
        "hatta sekmeyi kapatabilirsin."
    )
    for run in active:
        _render_active_run(run)


profile_runs = storage.list_runs(owner=profile["slug"])
if any(r["status"] in storage.ACTIVE_STATUSES for r in profile_runs):
    _live_runs_panel(profile["slug"])


# ===========================================================================
#                             TAMAMLANAN İŞLER
# ===========================================================================
def _render_image_grid(run: dict, images: list[tuple[dict, Path]]) -> None:
    cols_per_row = 4
    for row_start in range(0, len(images), cols_per_row):
        cols = st.columns(cols_per_row)
        for col, (item, path) in zip(cols, images[row_start : row_start + cols_per_row]):
            with col:
                st.image(str(path), caption=item["variation"][:60] or path.name, width="stretch")
                suffix = path.suffix.lstrip(".").lower()
                st.download_button(
                    label="💾 İndir",
                    # Callable: dosya sadece tıklanınca okunur; on_click="ignore":
                    # indirme sayfayı yeniden çalıştırmaz.
                    data=functools.partial(path.read_bytes),
                    file_name=path.name,
                    mime=f"image/{'jpeg' if suffix == 'jpg' else suffix}",
                    key=f"dl_{run['id']}_{item['key']}",
                    on_click="ignore",
                    width="stretch",
                )


def _render_finished_run(run: dict, show_images_default: bool, size_bytes: int) -> None:
    items = run["items"]
    out_dir = Path(run["output_dir"])
    images = [
        (item, out_dir / item["file"])
        for item in items
        if item["status"] == "ok" and item.get("file") and (out_dir / item["file"]).exists()
    ]
    ok_count = sum(1 for i in items if i["status"] == "ok")
    unfinished = [i for i in items if i["status"] in ("failed", "cancelled")]
    failed = [i for i in items if i["status"] == "failed"]
    price = PRICE_PER_IMAGE_BATCH if run["mode"] == "batch" else PRICE_PER_IMAGE_STANDARD

    size_text = _fmt_size(size_bytes)
    title = (
        f"{_status_icon(run['status'])} {storage.run_label(run)} · {_fmt_time(run['created_at'])} · "
        f"{MODE_LABELS[run['mode']]} · {ok_count}/{len(items)} görsel · {size_text} · "
        f"~${ok_count * price:.2f}"
    )

    # Prompt + varyasyon listesi görsellerin yanında da dursun (eski işlerde eksikse yaz).
    info_path = out_dir / storage.RUN_INFO_FILENAME
    if out_dir.exists() and not info_path.exists():
        try:
            storage.write_run_info(run)
        except OSError:
            pass

    with st.expander(title, expanded=show_images_default):
        if run.get("error"):
            st.error(run["error"])
        st.caption(f"📁 `{out_dir}`")

        # Bu üretim neyle yapılmıştı? Prompt ve varyasyonlar burada.
        with st.container(border=True):
            st.markdown("**📝 Master prompt**")
            st.code(
                run.get("master_prompt") or "(bu iş için prompt kaydı yok)",
                language=None,
                wrap_lines=True,
            )

            if items:
                st.markdown(f"**🔀 Varyasyonlar ({len(items)})**")
                st.dataframe(
                    [
                        {
                            "#": item["index"],
                            "Varyasyon": item["variation"] or "(metin yok)",
                            "Durum": ITEM_STATUS_LABELS.get(item["status"], item["status"]),
                            "Dosya / sebep": item.get("file") or item.get("error") or "",
                        }
                        for item in items
                    ],
                    hide_index=True,
                    width="stretch",
                )
                variation_text = "\n".join(i["variation"] for i in items if i["variation"])
                if variation_text:
                    st.caption("Varyasyon listesini kopyalamak için:")
                    st.code(variation_text, language=None)

            st.button(
                "↩️ Bu işi forma yükle",
                key=f"reuse_{run['id']}",
                on_click=_load_run_into_form,
                args=(run,),
                help=(
                    "Prompt'u, varyasyonları ve bu işte kullanılan referans görseli "
                    "forma geri yükler. Üretimi sen başlatırsın."
                ),
            )

        zip_col, toggle_col, delete_col = st.columns([3, 2, 2], vertical_alignment="center")
        if images:
            zip_col.download_button(
                label=f"📦 Hepsini ZIP olarak indir ({len(images)} görsel)",
                data=_zip_builder(
                    [path for _, path in images] + ([info_path] if info_path.exists() else [])
                ),
                file_name=f"gemini_{run['id']}.zip",
                mime="application/zip",
                key=f"zip_{run['id']}",
                on_click="ignore",
                width="stretch",
            )
        show_images = toggle_col.toggle(
            "Görselleri göster", value=show_images_default, key=f"show_{run['id']}"
        )
        confirm_key = f"confirm_delete_{run['id']}"
        delete_col.button(
            "🗑️ Sil",
            key=f"delete_{run['id']}",
            width="stretch",
            help="Bu üretimi ve klasöründeki görselleri kalıcı olarak siler.",
            on_click=_set_flag,
            args=(confirm_key, True),
        )
        if st.session_state.get(confirm_key):
            st.warning(
                f"**{storage.run_label(run)}** silinecek: {len(images)} görsel, {size_text}.\n\n"
                f"`{out_dir}` klasörü bilgisayardan da kaldırılır; geri alınamaz."
            )
            yes_col, no_col = st.columns(2)
            yes_col.button(
                "Evet, sil",
                key=f"delete_yes_{run['id']}",
                type="primary",
                width="stretch",
                on_click=_delete_runs,
                args=([run["id"]],),
            )
            no_col.button(
                "Vazgeç",
                key=f"delete_no_{run['id']}",
                width="stretch",
                on_click=_set_flag,
                args=(confirm_key, False),
            )

        if show_images and images:
            _render_image_grid(run, images)

        if failed:
            # Sebepler yukarıdaki tabloda; burada sadece sık karşılaşılan uyarı.
            if any(
                "429" in (i["error"] or "") or "RESOURCE_EXHAUSTED" in (i["error"] or "").upper()
                for i in failed
            ):
                st.info(
                    "**Rate Limit / Quota (429)**: Eş zamanlı istek sayısını düşür ve bekle; "
                    "günlük kota dolduysa yarın dene; free tier'da bu model çalışmaz → Billing aç."
                )
        retry_text = "\n".join(i["variation"] for i in unfinished if i["variation"])
        if retry_text:
            st.button(
                f"↩️ Tamamlanmayan {len(unfinished)} varyasyonu forma aktar",
                key=f"retry_{run['id']}",
                on_click=_set_variations,
                args=(retry_text,),
                help="Varyasyon kutusunu bu satırlarla doldurur; yeniden başlatmak sana kalır.",
            )


st.divider()
finished_runs = [r for r in profile_runs if r["status"] in storage.FINISHED_STATUSES]
run_sizes = {r["id"]: storage.run_folder_size(r) for r in finished_runs}
total_size = sum(run_sizes.values())
if finished_runs:
    st.subheader(f"🖼️ Üretimler ({len(finished_runs)} iş · {_fmt_size(total_size)})")
else:
    st.subheader("🖼️ Üretimler")
    st.caption("Henüz tamamlanan iş yok. Başlattığın işler bitince burada listelenir.")

delete_notice = st.session_state.pop("_delete_notice", None)
if delete_notice:
    st.toast(delete_notice, icon="🗑️")

visible_count = st.session_state.get("_visible_runs", 10)
visible_runs = finished_runs[:visible_count]
selected_ids = [r["id"] for r in visible_runs if st.session_state.get(f"select_{r['id']}")]

if visible_runs:
    select_col, action_col = st.columns([1, 2], vertical_alignment="center")
    select_col.checkbox(
        "Tümünü seç",
        key="select_all_runs",
        on_change=_toggle_select_all,
        args=(tuple(r["id"] for r in visible_runs),),
        help="Listede görünen işlerin hepsini işaretler.",
    )
    if selected_ids:
        selected_size = sum(run_sizes.get(i, 0) for i in selected_ids)
        action_col.button(
            f"🗑️ Seçilenleri sil ({len(selected_ids)} iş · {_fmt_size(selected_size)})",
            key="bulk_delete",
            type="primary",
            width="stretch",
            on_click=_set_flag,
            args=("_confirm_bulk_delete", True),
        )

    if st.session_state.get("_confirm_bulk_delete") and selected_ids:
        names = [storage.run_label(r, 40) for r in visible_runs if r["id"] in selected_ids]
        st.warning(
            f"**{len(selected_ids)} üretim** silinecek "
            f"({_fmt_size(sum(run_sizes.get(i, 0) for i in selected_ids))}):\n\n"
            + "\n".join(f"- {name}" for name in names)
            + "\n\nKlasörleri bilgisayardan da kaldırılır; geri alınamaz."
        )
        yes_col, no_col = st.columns(2)
        yes_col.button(
            "Evet, hepsini sil",
            key="bulk_delete_yes",
            type="primary",
            width="stretch",
            on_click=_delete_runs,
            args=(list(selected_ids),),
        )
        no_col.button(
            "Vazgeç",
            key="bulk_delete_no",
            width="stretch",
            on_click=_set_flag,
            args=("_confirm_bulk_delete", False),
        )

for position, finished_run in enumerate(visible_runs):
    check_col, run_col = st.columns([0.05, 0.95], vertical_alignment="top")
    check_col.checkbox(
        "Seç",
        key=f"select_{finished_run['id']}",
        label_visibility="collapsed",
        help="Toplu silmek için işaretle.",
    )
    with run_col:
        _render_finished_run(
            finished_run,
            show_images_default=position == 0,
            size_bytes=run_sizes[finished_run["id"]],
        )
if len(finished_runs) > visible_count:
    if st.button(f"Daha eski işleri göster ({len(finished_runs) - visible_count})"):
        st.session_state["_visible_runs"] = visible_count + 10
        st.rerun()
