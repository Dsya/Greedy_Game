# Greedy Kartu Remi — Android (Chaquopy)

Project ini membungkus `greedy_server.py` (server + frontend HTML/JS) menjadi
aplikasi Android menggunakan **Chaquopy** (Python SDK untuk Android). Saat
aplikasi dibuka, `MainActivity` menjalankan server Python secara lokal di
`127.0.0.1:8000` di dalam thread terpisah, lalu menampilkannya lewat WebView.

## Struktur penting

```
GreedyGame/
├── build.gradle                     # plugin Android + Chaquopy versi 17.0.0
├── settings.gradle
├── app/
│   ├── build.gradle                 # applicationId, minSdk 24, abiFilters
│   ├── src/main/
│   │   ├── AndroidManifest.xml
│   │   ├── java/com/rj/greedygame/MainActivity.java
│   │   ├── python/
│   │   │   ├── greedy_server.py     # server kamu, tidak diubah logikanya
│   │   │   └── bgm3.mp3             # ikut ter-bundle & terextract otomatis
│   │   └── res/mipmap-*/ic_launcher.png
└── .github/workflows/build-apk.yml  # build APK otomatis di GitHub Actions
```

## 1. Push ke GitHub

Dari folder `GreedyGame` ini:

```bash
git init
git add .
git commit -m "Initial commit: Greedy Kartu Remi Android (Chaquopy)"
git branch -M main
git remote add origin https://github.com/USERNAME/NAMA_REPO.git
git push -u origin main
```

Ganti `USERNAME/NAMA_REPO` dengan repo GitHub kamu (buat dulu repo kosong di
github.com kalau belum ada).

## 2. Build APK otomatis (GitHub Actions)

Begitu di-push, workflow `.github/workflows/build-apk.yml` otomatis jalan:
- Install JDK 17 + Android SDK
- Jalankan `gradle assembleDebug`
- APK hasil build diupload sebagai **artifact** bernama
  `greedy-game-debug-apk`

Cara ambil APK-nya: buka repo → tab **Actions** → klik run terbaru → scroll
ke bagian **Artifacts** → download `greedy-game-debug-apk` (isinya
`app-debug.apk`).

Kalau mau trigger manual tanpa push, buka tab **Actions** → pilih workflow
**Build APK** → **Run workflow**.

## 3. Build lokal via Android Studio (alternatif)

1. Buka folder `GreedyGame` di Android Studio (versi terbaru).
2. Android Studio otomatis membuatkan `gradlew`/wrapper saat sinkronisasi
   pertama kali (repo ini sengaja tidak menyertakan file biner wrapper).
3. Tunggu Gradle sync + Chaquopy mengunduh Python 3.10 runtime.
4. `Build > Build Bundle(s) / APK(s) > Build APK(s)`.
5. APK ada di `app/build/outputs/apk/debug/app-debug.apk`.

## Catatan

- Tidak perlu lisensi Chaquopy — sejak versi 15+ Chaquopy gratis dipakai di
  aplikasi apa pun.
- Server berjalan di `127.0.0.1:8000` di dalam HP itu sendiri, jadi fitur
  multiplayer LAN (join dari HP lain via IP lokal) tetap butuh HP ini dan
  HP lain berada di WiFi yang sama — persis seperti saat dijalankan di
  laptop.
- Kalau nanti mau tambah paket Python pihak ketiga (di luar standar
  library), tambahkan di `app/build.gradle`:
  ```groovy
  chaquopy {
      defaultConfig {
          version "3.10"
          pip {
              install "nama-paket"
          }
      }
  }
  ```
- `applicationId` saat ini `com.rj.greedygame` dan nama app "Greedy Kartu
  Remi" — ubah di `app/build.gradle` dan `strings.xml` kalau mau ganti.
