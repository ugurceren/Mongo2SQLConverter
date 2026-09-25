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

`--mode auto` (varsayılan kayıtlı tercih) şöyle karar verir:

- Yarım kalmış bir tam yükleme varsa ve kırılım ile tarih filtresi aynıysa, kontrol noktasından **devam eder**. Kolon genişlikleri her çalıştırmada yeniden örneklense de devam engellenmez; uzun değerler kolonu genişletir.
- Hedef kök tablo yoksa veya boşsa **tam senkron** yapar.
- Aksi hâlde **artımlı** çalışır: kontrol noktasındaki son `_id`'den sonrasını okur. ObjectId anahtarlarda 15 dakika geriden başlar, böylece saati geride kalan istemcilerin yazdığı belgeler kaçmaz.

Tek Windows görevi yeter; iki ayrı görev gerekmez.

İndirilen `.bat` / `.ps1` `--collection` ile birlikte `--table` (kök SQL tablosu) ve varsa `--rename yol=Tablo` taşır. Görev bu tablolara kilitlenir; uygulamada başka koleksiyon seçmek veya o koleksiyonun adını sonradan değiştirmek eski dosyayı etkilemez. Eski stil (yalnız `--collection`) hâlâ `config.local.yaml` kaydını okur.

```powershell
python tools/run_transfer.py --collection conversations --mode auto --schema dbo --table Conversations --rename "messages=ConvMessages"
```

`--mode full` tabloyu yeniden doldurur; yarım kalmış bir tam yükleme varsa önce onu bitirir. `--mode incremental` her zaman artımlıdır (işaret yoksa yine tüm belgeleri okur).

Diğer bayraklar:

| Bayrak | Ne yapar |
|--------|----------|
| `--restart` | Kontrol noktasını yok sayar ve yeni bir tam yükleme başlatır. Tablolar boş değilse her parti yazmadan önce siler. |
| `--max-rejects N` | N'den fazla belge reddedilirse işi durdurur. Varsayılan `loader.max_rejects` (1000). |
| `--full-profile` | `--sample 0` ile gerçekten her belgeyi profiller. Bu bayrak yoksa 1 milyonun üstündeki koleksiyonlarda profil 100.000 belgeyle sınırlanır. |
| `--preflight-only` | Hiçbir şey yazmaz; ön kontrol raporunu basar (aşağıda). |

Çıkış kodları:

| Kod | Anlamı |
|-----|--------|
| 0 | Temiz bitti. |
| 2 | Bitti, ama bazı belgeler reddedildi ya da değerler kırpıldı veya NULL'a çevrildi. Ayrıntı red dosyasında. |
| 1 | Hata. İş bir sonraki çalıştırmada kontrol noktasından devam eder. |

Bağlantı ve job ayarları `config.local.yaml` içindedir (koleksiyon başına `nesting`, `table`, `schema`, `schedule_mode`, `batch`, `sample`, kolon ve tarih tercihleri). SQL şifresi gerekiyorsa dosyada olmalı; Streamlit oturum şifresi CLI'da yoktur.

Kırılım (`nesting`) yalnız **Şema keşfi** sayfasında seçilir ve seçildiği anda kaydedilir. **SQL aktarımı** sayfası bu seçimi yalnız gösterir; değiştirmek için oradaki bağlantıyla Şema keşfine dönülür. Hiç seçilmemişse koleksiyonun yapısına göre varsayılan kullanılır ve "varsayılan" diye belirtilir.

**Windows Görev Zamanlayıcı:**

1. SQL aktarımı sayfasındaki **Zamanla** kartından komutu kopyalayın veya `.ps1` / `.bat` indirin. Dosya adı `mongo2sql_<koleksiyon>.bat`; içinde `--table` o koleksiyonun kök tablosudur.
2. Görev Zamanlayıcı → Görev Oluştur. Eylem: indirilen `.bat`, ya da Program `python.exe` (venv) ve karttaki tam argüman satırı. Başlangıç dizini proje klasörü.
3. **Windows — bu oturum** kimliği için görevi o Windows kullanıcısıyla ve "kullanıcı oturum açmış olsun" ile çalıştırın (Trusted Connection oturuma bağlıdır).
4. Çıkış kodu 0 temiz, 2 red ya da kırpmayla tamamlandı, 1 hata. Ayrıntı `logs/mongo2sql.log`.
5. Uzun işler (yüz milyonlarca belge günler sürebilir) için:
   - **Ayarlar** sekmesinde "Görevi şu süreden uzun çalışırsa durdur" varsayılanı **3 gündür**; kapatın ya da süreyi uzatın.
   - "Kullanıcı oturum açmış olsun ya da olmasın çalıştır"ı seçin ve parolayı saklayın ("Parolayı saklama" kutusunu işaretlemeyin). Oturum kapansa da iş sürer, Windows kimliği de ağda çalışır.
   - **Koşullar** sekmesinde "Yalnızca bilgisayar AC gücündeyse başlat" ile "Pile geçerse durdur" kutularını kaldırın. Windows'un güç ayarında uykuyu kapatın.
   - İş durursa ya da bilgisayar yeniden başlarsa aynı görevi tekrar çalıştırmak yeter; kaldığı yerden devam eder.

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

- Adlar ancak **Tablo adlarını kaydet** ile kaydedildikten sonra aktarıma ve Zamanla komutuna geçer. Kart, kaydedilmemiş bir değişiklik olduğunda bunu belirtir.
- İki tablo aynı adı alamaz. SQL Server varsayılan collation'da büyük/küçük harfi ayırt etmediği için `ConvTags` ile `convtags` da aynı ad sayılır. Kontrol noktası tablosunun adı (`Mongo2SqlCheckpoint`) da kullanılamaz. Kart böyle bir adı kaydetmez; elle yazılmış bir ayar olursa aktarım SQL'e dokunmadan durur.
- `--rename` anahtarı, kartın **kaynak** kolonundaki yoldur: `messages`, `messages[].attachments`. Sonda `[]` yazmak (`messages[]`) da kabul edilir. Hiçbir tabloya uymayan anahtar günlüğe uyarı olarak yazılır ve yok sayılır.
- Ad değiştirmek SQL'deki tabloyu yeniden adlandırmaz. Yeni adla yeni bir tablo oluşur; eski tablo ve satırları olduğu gibi kalır. Veriyi yeni ada taşımak için tabloyu SQL'de `sp_rename` ile yeniden adlandırın ya da yeni adlarla tabloları boşaltıp tam senkron çalıştırın.

Onceki surumler `conversations_messages` gibi adlar uretiyordu. Eski adlarla olusmus tablolariniz varsa yeni adlar ayri tablolar olur; eskilerini elle yeniden adlandirin ya da birakin.

## Aktarimi daraltma

**SQL aktarimi** sayfasinda koleksiyonun tamamini yazmak zorunlu degil.

**Tarih araligi:** Profilleme sirasinda bulunan tarih tipli alanlar (`createdAt`, `updatedAt` gibi) listelenir; index'li olanlar listenin basina gelir ve varsayilan secim olur. Birini secip baslangic ve bitis gunu verirsiniz. Bitis gunu dahildir. Gunler "Yerel saat" ya da "UTC" olarak yorumlanir ve Mongo'ya UTC olarak gider. Index'siz alanda min/max okunmaz (koleksiyon taramasi yapilmaz). Hizlandirmak icin:

```javascript
db.conversations.createIndex({ createdAt: 1 })
```

Aralik yalnizca yazmayi degil profillemeyi de daraltir, boylece kolon genisliklerini o donemin verisi belirler. Dizi elemani icindeki tarihlere gore filtreleme desteklenmez.

**Büyük koleksiyonda dönem yüklemesi (örneğin yıllık tam senkron):** Seçilen tarih alanı bir index'in ilk alanıysa hem profil örneği hem aktarım o index üzerinden okunur. Süre koleksiyonun büyüklüğüne değil, aralıktaki belge sayısına bağlıdır.
- Aktarım aralığı ~100.000 belgelik tarih pencerelerine böler; kontrol noktası pencerenin başını tutar.
- Kesilen iş yeniden başlatılınca o pencereyi baştan okur. O çalışmada her parti yazmadan önce kendi anahtarlarını siler, belge iki kez yazılmaz.
- Böyle bir yüklemeden sonraki `auto` çalışması, tablodaki en büyük `mongo_id`'den artımlı devam eder.
- Index yoksa aralık `_id` sırasıyla okunur ve sunucu koleksiyonun tamamına bakar. 350 milyonluk bir koleksiyonda bu, bir günlük aralık için bile saatler demektir. Index'i bir kez, yoğun olmayan bir saatte oluşturun: `db.<koleksiyon>.createIndex({ <tarihAlani>: 1 })`.

**Kolon secimi:** Kolonlar kartinda planin her kolonu ve alt tablosu tek tek kapatilabilir. `mongo_id`, alt tablo anahtarlari ve dizi sira kolonlari her zaman aktarilir. Bir alt tablonun butun kolonlari kapatilirsa o tablo hic olusturulmaz. Liste koleksiyonun profilinden gelir; koleksiyon ve kok tablo secildigi anda profil otomatik cikarilir.

Iki secim de `config.local.yaml` icine koleksiyon basina yazilir, uygulama yeniden acildiginda geri yuklenir. Ayni kayda kirilim, kok tablo, sema, yazma partisi, `table_names` ve `schedule_mode` da eklenir; CLI / Gorev Zamanlayici bunlari okur.

**Dikkat:** Artimli senkron ile tarih araligini birlikte kullanirken belgeler `_id` sirasiyla okunur ve isaret yalnizca filtreden gecen son belgeye ilerler; aralik disinda kalan daha buyuk `_id`'ler sonraki kosularda bir daha okunmaz. Donem bazli yukleme icin tam senkron daha guvenlidir.

## Büyük koleksiyonlar

Yüz milyonlarca belgelik bir yükleme günler sürebilir. Aktarım bu süre boyunca kopmalara, bozuk belgelere ve yeniden başlatmalara dayanacak şekilde çalışır.

**Kontrol noktası.** Aktarım, hedef şemada `[şema].[Mongo2SqlCheckpoint]` tablosunu kendisi oluşturur; kök tablo başına bir satır tutar. Bu satır her partide, verilerle aynı transaction'da güncellenir. Böylece iş nerede durursa dursun, satır SQL'e hangi belgelerin yazıldığını tam olarak gösterir. Yeniden çalıştırılınca iş, belge atlamadan ve çift yazmadan kaldığı yerden devam eder.
- Son `_id` her tipte kayıpsız saklanır: ObjectId, sayı ya da metin.
- Yazma yetkisi olup tablo oluşturma yetkisi olmayan hesaplarda tabloyu DBA oluşturabilir. Gereken `CREATE TABLE` komutu, ilk çalıştırmada günlüğe yazılır.
- Aynı tablolara aynı anda tek bir iş yazar (`sp_getapplock`). Arayüz ile Görev Zamanlayıcı çakışırsa ikinci iş hemen "başka bir aktarım yazıyor" hatasıyla durur.

**Yeniden deneme.**
- Beklenip yeniden denenen hatalar:
  - Mongo tarafında bağlantı kopması, primary değişimi ve imleç kaybı.
  - SQL tarafında bağlantı kopması, deadlock ve kilit zaman aşımı.
- Deneme sınırı olay başına en çok 12 deneme ya da 30 dakikadır.
- Transaction log ya da disk dolarsa (9002) iş 2 saate kadar 5 dakikada bir yeniden dener ve günlüğe DBA için not düşer.
- Commit sunucuya ulaşmış ama onayı kaybolmuşsa bunu kontrol noktasından anlar ve o partiyi tekrar yazmaz.

**Bozuk veri.** SQL Server'ın kabul etmediği bir belge çıkarsa (aralık dışı sayı, kısıtlama ihlali vb.) iş durmaz. Parti ikiye bölünerek o belge bulunur ve yalnız o atlanır.
- NaN, taşan sayılar ve SQL Server'ın desteklediği aralığın dışındaki tarihler NULL yazılır ve kaydedilir.
- Çözülemeyen BSON içeren belgeler reddedilir.
- Hepsi `logs/rejects/<koleksiyon>__<tablo>__<zaman>.jsonl` dosyasına satır satır yazılır: `_id`, tablo, kolon, SQLSTATE ve neden. Belgenin içeriği bu dosyaya yazılmaz.
- Reddedilen belge sayısı `loader.max_rejects` değerini aşarsa ya da tek partide 50'den fazla red çıkarsa hata sistematik sayılır ve iş durur.

**Uzun metin.** Profilde görülenden uzun bir değer gelirse kolon kırpılmadan genişletilir (`ALTER COLUMN`). Nullability ve collation korunur. Anahtar, index ya da FK kolonlarında ya da ALTER yetkisi yoksa genişletme yapılamaz; değer kırpılır ve red dosyasına `clip` olarak yazılır.

**Boşaltma.** "Yazmadan önce tabloları boşalt" tek transaction'da `TRUNCATE` kullanır: alt tabloların FK'ları düşürülür, tablolar boşaltılır, FK'lar aynı adlarla geri eklenir. Yetki yoksa 50.000'lik `DELETE` parçalarına düşer.

**Ön kontrol.** Büyük bir işten önce SQL aktarımı sayfasındaki **Ön kontrol** düğmesini kullanın, ya da:

```powershell
python tools/run_transfer.py --collection conversations --preflight-only
```

Rapor şunları içerir:
- ODBC sürücüsü: eski "SQL Server" sürücüsü için uyarı verir.
- SQL sürümü ve collation.
- Recovery model, log bekleme nedeni, dosya büyüme ayarları ve yetkiler.
- Mongo sürümü ve tarih alanının index'i.
- 5.000 belgelik gerçek bir hız ölçümü: okuma, düzleştirme ve geçici (`#temp`) tablolara yazıp geri alma. Hiçbir şey kalıcı olarak yazılmaz.

Aşama hızlarından toplam süre tahmin edilir. İş başladıktan sonra `logs/mongo2sql.log` içindeki ilerleme satırları gerçek hızı ve kalan süreyi gösterir.

**Ayarlar** (`config.yaml` → `loader:`):

| Ayar | Varsayılan | Ne işe yarar |
|------|------------|--------------|
| `max_rejects` | 1000 | Bu sayının üstünde red olursa iş durur. |
| `incremental_overlap_minutes` | 15 | Artımlı çalışma ObjectId'lerde bu kadar geriden başlar. |
| `commit_rows`, `commit_mb` | 20000, 16 | Yazma partisi (belge sayısı), bu satır sayısı ya da bu boyut, hangisi önce dolarsa commit edilir. |
| `prefetch` | `true` | SQL yazarken sonraki partiyi ayrı bir iş parçacığında okur ve düzleştirir. |

**Ağ ve hız.** Veri Mongo'dan işi çalıştıran makineye, oradan SQL Server'a gider. Bu yüzden hızı çoğu zaman o makinenin sunuculara bağlantısı belirler: VPN, Wi-Fi ya da başka bir ofis üzerinden saniyede birkaç yüz belge, aynı veri merkezinde ise binlerce belge beklenir. Büyük işleri sunuculara yakın bir makinede **Zamanla** komutuyla çalıştırın.

- Mongo trafiği varsayılan olarak sıkıştırılır. zlib her zaman kullanılabilir; zstd, pymongo bu Python'da kullanabiliyorsa önce denenir (Python 3.14 ile pymongo 4.17'de ek paket gerekmez). URI'de `compressors=` yazarsanız o kullanılır. Sunucu sıkıştırmayı desteklemiyorsa bağlantı sıkıştırmasız devam eder.
- Günlükteki ilerleme ve bitiş satırları süreyi ayırır:
  - Mongo tarafı: `okuma_sn`, `okuma_mb`, `okuma_mb_sn`.
  - İşlemci: `düzleştirme_sn`.
  - SQL: `ekleme_sn` ve `commit_sn`.
- Okuma ile yazma aynı anda sürdüğü için bu süreler toplamı aşar. İkisi de toplam süreye yakın ve MB/sn düşükse darboğaz ağdır.
- Aynı özet, arayüzdeki Sonuç kartında "Süre dağılımı" olarak görünür. Ön kontrol de iki tarafın MB/sn değerini ölçer.

**SQL Server log'u.** Recovery model FULL ise transaction log yalnız log yedeğiyle boşalır. Uzun bir yüklemede DBA'nın sık log yedeği planlaması gerekir. Araç recovery model'i değiştirmez.

**Hız ölçümü (veritabanı olmadan).** Sentetik belgelerle BSON çözme ve düzleştirme hızını ölçer; derlenmiş düzleştiricinin çıktısını referansla karşılaştırır:

```powershell
python tools/bench_transfer.py --docs 20000 --nesting all
```

## Yapi

- `core/inspect.py` — sema profilleme, DRDL/DDL
- `core/logutil.py` — `logs/mongo2sql.log` dosya günlüğü
- `core/transfer.py` — aktarım akışı: okuma, düzleştirme ve yazma (önceki `flatten_document` referans olarak durur)
- `core/convert.py` — plandan derlenen, kolon başına dönüştürücülerle düzleştirme
- `core/reader.py` — `_id` sırasıyla, parça parça ve yeniden başlatılabilir Mongo okuması
- `core/writer.py` — hepsi-ya-hiçbiri partiler, ikiye bölme, kolon genişletme
- `core/checkpoint.py` — kontrol noktası tablosu ve çalıştırmanın nereden başlayacağı
- `core/retry.py` — hata sınıfları ve bekleme süreleri
- `core/rejects.py` — `logs/rejects/` red dosyaları
- `core/preflight.py` — ön kontrol raporu ve hız ölçümü
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
- `tools/bench_transfer.py` — veritabanısız hız ölçümü ve çıktı karşılaştırması
- `tests/` — sahte Mongo ve SQL Server'la testler (ek bağımlılık yok)

## Testler

```powershell
python -m unittest discover -s tests -t . -v
```

Testler gerçek bir veritabanı istemez. Kapsadıkları:
- Dönüştürücünün önceki sürümle eşdeğerliği.
- Okuyucunun karışık tipli `_id`'lerde sırası ve kopmalardan sonra yeniden başlaması.
- Yazıcıda ikiye bölme, deadlock, dolu log ve onayı kaybolan commit.
- Durdurup devam ettirmede her belgenin tam bir kez yazılması.
- Gönderilen SQL metinleri.

Gerçek SQL Server ve Mongo davranışı ayrıca bir deneme ortamında doğrulanmalıdır.

## Ayirma

Bu repoya **alınmayan** parcalar: `sync.py`, `flatten*.py`, `streams.py`, `scheduler/`, stream-spesifik SQL scriptleri. Sync icin SelimMongoDBtoSQL kullanilmaya devam eder.
