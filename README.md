# Mongo2SQL Converter

MongoDB collection'larini tarayip olculere dayali **DRDL** ve **DDL** ureten arac.

[SelimMongoDBtoSQL](https://github.com/ugurceren/SelimMongoDBtoSQL) sync projesinden **ayri** bir urundur.

| SelimMongoDBtoSQL | Mongo2SQLConverter |
|-------------------|-------------------|
| Sabit stream'ler (hybrid_conversations, conversations) | Herhangi bir collection |
| Elle flatten + MERGE sync | Otomatik sema cikarimi |
| Zamanlanmis batch | DRDL / DDL uretimi |

## Kurulum

```powershell
cd "D:\Code and Business\Mongo2SQLConverter"
pip install -r requirements.txt
```

Baglanti bilgileri `config.yaml` icinde yoktur. Streamlit'te **Baglantilar** sayfasindan doldurun; kayit `config.local.yaml` dosyasina yazilir (git'e eklenmez). Elle yazmak icin:

```powershell
copy config.local.example.yaml config.local.yaml
```

Ornek dosyada `mydb`, `user`, `srv\INSTANCE` gibi yer tutucular vardir; gercek sifre ve sunucu adini kendiniz yazin.

## SQL kimlik dogrulama

**Baglantilar** sayfasindaki **Yontem** listesi, yazma yetkisi olan hesabi secmenizi saglar:

| Yontem | Nasil baglanir | Kullanici alani |
|--------|----------------|-----------------|
| Windows — bu oturum | `Trusted_Connection=yes`, uygulamayi calistiran hesap | gerekmez |
| SQL Server hesabi | `UID` + `PWD` | login adi (domain oneki yazmayin) |
| Windows — baska hesap | Girilen domain hesabi baglanti aninda taklit edilir | `DOMAIN\hesap` |

**Windows — baska hesap** modu `pywin32` ister (`requirements.txt` icinde). ODBC domain kullanici/sifresini baglanti dizesinde tasiyamadigi icin hesap `LogonUser` ile taklit edilir; sadece baglanti kurulurken gecerlidir.

**Sifreleme:** Driver 18 varsayilan olarak sifreler ve sertifikayi dogrular; kurum CA'si yoksa "sertifika zinciri" hatasi verir. **Sunucu sertifikasina dogrulamadan guven** kutusu (`TrustServerCertificate=yes`) ya da Driver 17 bunu cozer.

**Sifre saklama:** "Sifreyi bu makinede sakla" kapatilirsa sifre `config.local.yaml`'a yazilmaz, yalnizca acik oturumda tutulur. Domain hesaplari icin kapali tutmak onerilir.

**Baglantiyi dene** yalnizca baglanmakla kalmaz; oturum adini, rolleri ve aktarimin ihtiyac duydugu yetkileri (tablo olusturma, hedef semaya yazma) raporlar. Eksik yetki varsa hangi rolun gerektigini soyler:

```sql
ALTER ROLE db_datareader ADD MEMBER [svc_mongo2sql];
ALTER ROLE db_datawriter ADD MEMBER [svc_mongo2sql];
ALTER ROLE db_ddladmin   ADD MEMBER [svc_mongo2sql];
```

`db_ddladmin` yalnizca tablolari uygulama olusturacaksa gerekir.

## Kullanim

**Streamlit:**

```powershell
python run.py
```

Aktarım ve profil satırları `logs/mongo2sql.log` dosyasına yazılır (başlangıç, ilerleme, bitiş, hata). Dosya 10 MB olunca döner. `logs/` git'e eklenmez.

Üst şeritteki ay/güneş düğmesi Streamlit'in kendi koyu/açık temasını seçer (`.streamlit/config.toml` içindeki `[theme.dark]` / `[theme.light]`). Seçim bu tarayıcıda kalır; geçişte sayfa bir kez yenilenir, arka planda süren aktarım etkilenmez. Seçim yapılmamışsa işletim sisteminin teması kullanılır.

Tema değişikliklerini veritabanı olmadan denemek için galeri:

```powershell
python -m streamlit run tools/ui_gallery.py --server.port 8512
```

**CLI (aktarım / Görev Zamanlayıcı):**

```powershell
python tools/run_transfer.py --collection conversations --mode auto
```

`--mode auto` (varsayılan kayıtlı tercih): hedef kök tablo yoksa veya boşsa **tam senkron**, tabloda satır varsa **artımlı** (`mongo_id` son `_id`'den büyük). Tek Windows görevi yeter; iki ayrı görev gerekmez.

İndirilen `.bat` / `.ps1` `--collection` ile birlikte `--table` (kök SQL tablosu) ve varsa `--rename yol=Tablo` taşır. Görev bu tablolara kilitlenir; uygulamada başka koleksiyon seçmek veya o koleksiyonun adını sonradan değiştirmek eski dosyayı etkilemez. Eski stil (yalnız `--collection`) hâlâ `config.local.yaml` kaydını okur.

```powershell
python tools/run_transfer.py --collection conversations --mode auto --schema dbo --table Conversations --rename "messages[]=ConvMessages"
```

`--mode full` tabloyu yeniden doldurur. `--mode incremental` her zaman artımlıdır (işaret yoksa yine tüm belgeleri okur).

Bağlantı ve job ayarları `config.local.yaml` içindedir (koleksiyon başına `nesting`, `table`, `schema`, `schedule_mode`, `batch`, `sample`, kolon ve tarih tercihleri). SQL şifresi gerekiyorsa dosyada olmalı; Streamlit oturum şifresi CLI'da yoktur.

**Windows Görev Zamanlayıcı:**

1. SQL aktarımı sayfasındaki **Zamanla** kartından komutu kopyalayın veya `.ps1` / `.bat` indirin. Dosya adı `mongo2sql_<koleksiyon>.bat`; içinde `--table` o koleksiyonun kök tablosudur.
2. Görev Zamanlayıcı → Görev Oluştur. Eylem: indirilen `.bat`, ya da Program `python.exe` (venv) ve karttaki tam argüman satırı. Başlangıç dizini proje klasörü.
3. **Windows — bu oturum** kimliği için görevi o Windows kullanıcısıyla ve "kullanıcı oturum açmış olsun" ile çalıştırın (Trusted Connection oturuma bağlıdır).
4. Çıkış kodu 0 başarı, 1 hata. Ayrıntı `logs/mongo2sql.log`.

Periyodik görevde tarih aralığını kapatın. Artımlı + sabit tarih birlikte kullanıldığında aralık dışı `_id`'ler sonraki koşularda kaçabilir; dönem yüklemesi için tam senkron daha güvenlidir. `auto` artımlı aşamada kayıtlı tarih filtresini uygulamaz.

**CLI (DRDL):**

```powershell
python tools/infer_schema.py --collection conversations --out-drdl conversations.drdl
python tools/infer_schema.py --collection conversations --sample 5000 --out-ddl draft.sql
python tools/infer_schema.py --from-file export.json --collection mycol --out-drdl out.drdl
```

## Tablo adlari

Tablo adlari **PascalCase** uretilir: `hybrid_conversations` koleksiyonu `HybridConversations` tablosu olur. Alt tablolar kok addan ayirici kullanmadan turer, yani `conversations` icindeki `messages` dizisi `ConversationsMessages` olur.

**Kolon adlari degismez.** Mongo alan adi neyse kolon adi odur (`createdAt` -> `createdAt`), boylece kolona bakip hangi alandan geldigi anlasilir.

Kok tablo adini **SQL aktarimi** sayfasindaki **Kok tablo** kutusundan ya da **Tablo adlari** listesinden degistirebilirsiniz; ne yazarsaniz yazin PascalCase'e cevrilir. Alt tablolar varsayilan olarak o ada gore yeniden adlandirilir (`Conversations` + `messages` -> `ConversationsMessages`). Liste icindeki **SQL adi** kolonundan her tabloyu ayri ayri yazabilirsiniz; uretilen ada esit birakanlar kok degisince yeniden turer. Ozel adlar `config.local.yaml` icinde koleksiyon basina `table_names` olarak saklanir. Zamanlanan `.bat` / `.ps1` indirme anindaki tablo adlarini `--table` / `--rename` olarak gomdugu icin her gorev kendi tablosuna yazar.

Onceki surumler `conversations_messages` gibi adlar uretiyordu. Eski adlarla olusmus tablolariniz varsa yeni adlar ayri tablolar olur; eskilerini elle yeniden adlandirin ya da birakin.

## Aktarimi daraltma

**SQL aktarimi** sayfasinda koleksiyonun tamamini yazmak zorunlu degil.

**Tarih araligi:** Profilleme sirasinda bulunan tarih tipli alanlar (`createdAt`, `updatedAt` gibi) listelenir; index'li olanlar listenin basina gelir ve varsayilan secim olur. Birini secip baslangic ve bitis gunu verirsiniz. Bitis gunu dahildir. Gunler "Yerel saat" ya da "UTC" olarak yorumlanir ve Mongo'ya UTC olarak gider. Index'siz alanda min/max okunmaz (koleksiyon taramasi yapilmaz). Hizlandirmak icin:

```javascript
db.conversations.createIndex({ createdAt: 1 })
```

Aralik yalnizca yazmayi degil profillemeyi de daraltir, boylece kolon genisliklerini o donemin verisi belirler. Dizi elemani icindeki tarihlere gore filtreleme desteklenmez.

**Kolon secimi:** Kolonlar kartinda planin her kolonu ve alt tablosu tek tek kapatilabilir. `mongo_id`, alt tablo anahtarlari ve dizi sira kolonlari her zaman aktarilir. Bir alt tablonun butun kolonlari kapatilirsa o tablo hic olusturulmaz. Liste koleksiyonun profilinden gelir; koleksiyon ve kok tablo secildigi anda profil otomatik cikarilir.

Iki secim de `config.local.yaml` icine koleksiyon basina yazilir, uygulama yeniden acildiginda geri yuklenir. Ayni kayda kirilim, kok tablo, sema, yazma partisi, `table_names` ve `schedule_mode` da eklenir; CLI / Gorev Zamanlayici bunlari okur.

**Dikkat:** Artimli senkron ile tarih araligini birlikte kullanirken belgeler `_id` sirasiyla okunur ve isaret yalnizca filtreden gecen son belgeye ilerler; aralik disinda kalan daha buyuk `_id`'ler sonraki kosularda bir daha okunmaz. Donem bazli yukleme icin tam senkron daha guvenlidir.

## Yapi

- `core/inspect.py` — sema profilleme, DRDL/DDL
- `core/logutil.py` — `logs/mongo2sql.log` dosya günlüğü
- `core/transfer.py` — plana gore flatten + MSSQL'e yazma
- `core/mongo.py` — Mongo baglantisi
- `core/mssql.py` — MSSQL baglantisi
- `core/run_job.py` — Streamlit'siz profil + aktarım (CLI)
- `core/settings.py` — config.yaml + config.local.yaml
- `app/main.py` — Streamlit kabugu (gezinme, durum)
- `app/ui/theme.py` — stil ve ortak arayuz parcalari
- `app/ui/services.py` — arayuzun kullandigi baglanti/profil yardimcilari
- `app/ui/discovery.py` — Sema kesfi sayfasi (SQL gerekmez)
- `app/ui/transfer.py` — SQL aktarimi sayfasi
- `app/ui/connections.py` — Baglantilar sayfasi
- `tools/infer_schema.py` — sema CLI
- `tools/run_transfer.py` — aktarım CLI (Görev Zamanlayıcı)
- `tools/ui_gallery.py` — tema galerisi: arayüz bileşenleri statik veriyle, iki temada

## Ayirma

Bu repoya **alınmayan** parcalar: `sync.py`, `flatten*.py`, `streams.py`, `scheduler/`, stream-spesifik SQL scriptleri. Sync icin SelimMongoDBtoSQL kullanilmaya devam eder.
