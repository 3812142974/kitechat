package com.kitechat.client;

import android.Manifest;
import android.annotation.SuppressLint;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.net.Uri;
import android.os.Bundle;
import android.provider.MediaStore;
import android.util.Base64;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import androidx.activity.OnBackPressedCallback;
import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.content.FileProvider;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import java.io.File;
import java.util.ArrayList;
import java.util.List;

/**
 * KiteChat Android shell.
 *
 * Loading strategy:
 *   1. https://<host>:<tls_port>/  (secure context — required for the
 *      scan-code camera feature; our mkcert CA is pinned via
 *      res/xml/network_security_config.xml)
 *   2. http://<host>:<port>/       (plain port, WebSocket still works)
 *   3. bundled file:// copy with config in the URL fragment
 *
 * Camera: requested automatically at startup (runtime permission on
 * Android 6+). WebView permission requests (getUserMedia) are granted for
 * video capture.
 */
public class MainActivity extends AppCompatActivity {

    private static final String OBF_KEY = "n0v4ch4t$cfg";
    private static final int CAMERA_REQ = 1001;
    private static final int PICKER_CAMERA_REQ = 1002;

    private WebView webView;
    private boolean fellBack = false;      // -> http fallback
    private boolean fellBackFile = false;  // -> bundled file:// fallback
    private PermissionRequest pendingCameraGrant;
    /** Real status-bar/cutout height in dp, measured from window insets. */
    private volatile int measuredTopDp = -1;
    /** Real navigation-bar (gesture bar) height in dp. */
    private volatile int measuredBottomDp = -1;
    // last raw values, for the one-time inset debug readout
    private volatile int lastWebViewTopPx = -1;
    private volatile int lastStatusPx = -1;
    private volatile int lastCutPx = -1;
    private volatile int lastRectCount = -1;
    private volatile float lastDensity = -1f;
    private final long bootMs = System.currentTimeMillis();
    private volatile boolean diagSent = false;

    // ---- avatar/file picker (lazy: only fires when the page opens <input type=file>) ----
    private ValueCallback<Uri[]> filePathCallback;
    private Uri cameraCaptureUri;
    private WebChromeClient.FileChooserParams pendingChooserParams;
    private final ActivityResultLauncher<Intent> fileChooserLauncher =
        registerForActivityResult(new ActivityResultContracts.StartActivityForResult(), result -> {
            if (filePathCallback == null) return;
            Uri[] results = null;
            Intent data = result.getData();
            if (result.getResultCode() == RESULT_OK) {
                if (data != null && data.getData() != null) {
                    results = new Uri[]{data.getData()};
                } else if (data != null && data.getClipData() != null) {
                    int n = data.getClipData().getItemCount();
                    results = new Uri[n];
                    for (int i = 0; i < n; i++) results[i] = data.getClipData().getItemAt(i).getUri();
                } else if (cameraCaptureUri != null) {
                    results = new Uri[]{cameraCaptureUri};
                }
            }
            filePathCallback.onReceiveValue(results);
            filePathCallback = null;
            cameraCaptureUri = null;
        });

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        webView = new WebView(this);
        setContentView(webView);

        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(true);
        s.setAllowFileAccessFromFileURLs(true);
        s.setAllowUniversalAccessFromFileURLs(true);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);

        // Native bridge: lets the page learn the real status-bar / camera
        // cutout height (env(safe-area-inset-top) returns 0 in many
        // WebViews, which made the banner sit ON the punch-hole), and
        // drives the in-app self-updater (scan / download / install).
        webView.addJavascriptInterface(new Object() {
            @android.webkit.JavascriptInterface
            public int topInsetDp() {
                // Real status-bar + cutout height in dp, measured from
                // window insets (see setOnApplyWindowInsetsListener below).
                // -1 = not measured yet; the page keeps its CSS fallback.
                return measuredTopDp;
            }

            @android.webkit.JavascriptInterface
            public int bottomInsetDp() {
                return measuredBottomDp;
            }

            @android.webkit.JavascriptInterface
            public String installedVersion() {
                try {
                    return getPackageManager().getPackageInfo(
                            getPackageName(), 0).versionName;
                } catch (Exception e) {
                    return "";
                }
            }

            @android.webkit.JavascriptInterface
            public String scanUpdates() {
                // JSON: leftover APKs of the same version are deleted here
                return Updater.scanLocalApks(MainActivity.this);
            }

            @android.webkit.JavascriptInterface
            public void downloadAndInstall(String url, String filename) {
                Updater.downloadAndInstall(MainActivity.this, url, filename);
            }

            @android.webkit.JavascriptInterface
            public void installLocal(String path) {
                java.io.File apk = new java.io.File(path);
                if (apk.exists()) {
                    MainActivity.this.runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            Updater.launchInstallerLocal(
                                    MainActivity.this, apk);
                        }
                    });
                }
            }
        }, "KCNative");

        applyThemeBackground();
        // NOTE: camera permission is intentionally NOT requested at startup —
        // it is requested lazily the first time the scan-code feature needs
        // it (see onPermissionRequest below).

        // Measure the REAL status-bar + cutout height once the window is
        // laid out and push it to the page (dp == CSS px in a WebView at
        // scale 1). Some ROMs report an inflated "status_bar_height"
        // resource, which pushed the topbar far below the status bar —
        // window insets are the ground truth.
        ViewCompat.setOnApplyWindowInsetsListener(webView, (v, insets) -> {
            final int statusPx =
                    insets.getInsets(WindowInsetsCompat.Type.statusBars()).top;
            // Cutout: trust it ONLY when the ROM can point at a REAL hole
            // (non-empty bounding rects). Some ROMs report phantom
            // displayCutout insets on screens without any cutout (verified
            // by screenshot pixel analysis — no punch-hole existed), and
            // adding that phantom value pushed the topbar ~60dp too low.
            int cutPx = 0;
            androidx.core.view.DisplayCutoutCompat cutout = insets.getDisplayCutout();
            final int rectCount = (cutout == null) ? 0 : cutout.getBoundingRects().size();
            if (cutout != null && !cutout.getBoundingRects().isEmpty()) {
                cutPx = cutout.getSafeInsetTop();
            }
            // status bar and a real cutout OVERLAP (the hole sits inside
            // the status-bar strip) — take the max, never the sum.
            final int topPxRaw = Math.max(statusPx, cutPx);
            final int bottomPx =
                    insets.getInsets(WindowInsetsCompat.Type.navigationBars()).bottom;
            final int fStatusPx = statusPx, fCutPx = cutPx, fRects = rectCount;
            // position check must run after layout
            v.post(() -> {
                int topPx = topPxRaw;
                // If the WebView itself starts BELOW the top of the screen
                // the window is not edge-to-edge on this ROM — the system
                // has ALREADY reserved the status-bar/cutout space in its
                // layout, so the page must not add ANY extra top offset.
                // (Comparing against the reported inset is unreliable: some
                // ROMs report inflated inset values that don't match the
                // space they actually reserved.)
                int[] loc = new int[2];
                v.getLocationOnScreen(loc);
                lastWebViewTopPx = loc[1];
                lastStatusPx = fStatusPx;
                lastCutPx = fCutPx;
                lastRectCount = fRects;
                if (loc[1] > 0) topPx = 0;
                float density = getResources().getDisplayMetrics().density;
                lastDensity = density;
                int topDp = Math.round(topPx / Math.max(0.001f, density));
                int bottomDp = Math.round(bottomPx / Math.max(0.001f, density));
                if (topDp != measuredTopDp || bottomDp != measuredBottomDp) {
                    measuredTopDp = topDp;
                    measuredBottomDp = bottomDp;
                    runJs("if (window.__kcApplySafeInsets) window.__kcApplySafeInsets("
                            + topDp + "," + bottomDp + ");");
                }
            });
            return insets;
        });

        // System back button / edge-swipe gesture: hand it to the web app
        // first (it closes popups, overlay pages and the open chat, in
        // that order); only exit the activity when nothing is left to
        // close. Without this the gesture quit the app outright because
        // the in-app pages are overlays, not WebView history entries.
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                if (webView.canGoBack()) { webView.goBack(); return; }
                webView.evaluateJavascript(
                    "(function(){ try { return (typeof __kcHandleBack === 'function') ? !!__kcHandleBack() : false; } catch (e) { return false; } })()",
                    value -> {
                        boolean consumed = value != null && value.contains("true");
                        if (!consumed) {
                            // nothing open in the web app -> default back
                            // behaviour (finish the activity)
                            setEnabled(false);
                            getOnBackPressedDispatcher().onBackPressed();
                            setEnabled(true);
                        }
                    });
            }
        });

        final String fallbackUrl = buildFallbackUrl();
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request.isForMainFrame()) {
                    String failed = request.getUrl().toString();
                    if (!fellBack && failed.startsWith("https://")) {
                        // TLS not trusted / TLS port down -> retry on plain http
                        fellBack = true;
                        String plain = readServerUrl();
                        if (plain != null) { view.loadUrl(plain + "/"); return; }
                    }
                    if (!fellBackFile) {
                        fellBackFile = true;
                        view.loadUrl(fallbackUrl);
                    }
                }
                super.onReceivedError(view, request, error);
            }

            @Override
            public void onReceivedSslError(WebView view,
                    android.webkit.SslErrorHandler handler,
                    android.net.http.SslError error) {
                // Do NOT proceed past an untrusted certificate — fall back
                // to the plain-HTTP port instead (connection works, camera
                // just stays unavailable there).
                handler.cancel();
                if (!fellBack) {
                    fellBack = true;
                    String plain = readServerUrl();
                    if (plain != null) { view.loadUrl(plain + "/"); return; }
                }
                if (!fellBackFile) {
                    fellBackFile = true;
                    view.loadUrl(buildFallbackUrl());
                }
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                // Clear WebView history once the main page is up. Without
                // this, an https->http fallback (or any redirect) leaves a
                // history entry, and the system back gesture would call
                // goBack() (reloading the failed URL) instead of closing
                // the in-app overlay the user is looking at.
                view.clearHistory();
                // one-time diagnostics: wait a moment so window insets have
                // been applied, then report real numbers to the server log
                if (diagSent || url == null || url.startsWith("file:")) return;
                diagSent = true;
                final String pageUrl = url;
                view.postDelayed(() -> postDiag(pageUrl), 2000);
            }
        });
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(PermissionRequest request) {
                // Lazy camera permission: only when the page actually asks
                // (scan-code feature), not at startup.
                boolean wantsCamera = false;
                for (String r : request.getResources()) {
                    if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(r)) {
                        wantsCamera = true;
                        break;
                    }
                }
                if (!wantsCamera) { request.deny(); return; }
                if (checkSelfPermission(Manifest.permission.CAMERA)
                        == PackageManager.PERMISSION_GRANTED) {
                    request.grant(new String[]{PermissionRequest.RESOURCE_VIDEO_CAPTURE});
                    return;
                }
                // defer the web permission until the Android runtime
                // permission dialog is decided
                pendingCameraGrant = request;
                requestPermissions(new String[]{Manifest.permission.CAMERA},
                        CAMERA_REQ);
            }

            @Override
            public boolean onShowFileChooser(WebView view,
                    ValueCallback<Uri[]> callback, FileChooserParams params) {
                // Lazy permission model: this fires ONLY when the page clicks
                // <input type="file"> (avatar change). Photo/gallery
                // permissions are requested here at the moment of need,
                // never at app startup.
                if (filePathCallback != null) {
                    filePathCallback.onReceiveValue(null);
                }
                filePathCallback = callback;
                openFileChooser(params);
                return true;
            }
        });

        String tlsUrl = extractJsonString(readConfigJson(), "server_url_tls");
        String serverUrl = readServerUrl();
        String scheme = extractJsonString(readConfigJson(), "scheme");
        if (scheme == null || scheme.isEmpty()) scheme = "auto";
        tlsUrl = stripSlash(tlsUrl);
        if ("http".equals(scheme)) {
            // dev environment: plain HTTP only — no TLS handshake, fastest
            // startup. Scan-code camera is unavailable on http (needs a
            // secure context); pick "auto" or "https" for production.
            if (serverUrl != null) { webView.loadUrl(serverUrl + "/"); }
            else { webView.loadUrl(fallbackUrl); }
        } else if ("https".equals(scheme)) {
            // production: HTTPS only (camera works); fall back to http
            // only if the TLS port is unreachable (onReceivedError path).
            if (tlsUrl != null && tlsUrl.startsWith("https://")) {
                webView.loadUrl(tlsUrl + "/");
            } else if (serverUrl != null) {
                webView.loadUrl(serverUrl + "/");
            } else {
                webView.loadUrl(fallbackUrl);
            }
        } else {
            // auto (default): try HTTPS first, fall back to HTTP
            if (tlsUrl != null && tlsUrl.startsWith("https://")) {
                webView.loadUrl(tlsUrl + "/");
            } else if (serverUrl != null) {
                webView.loadUrl(serverUrl + "/");
            } else {
                webView.loadUrl(fallbackUrl);
            }
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode,
            String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == CAMERA_REQ && pendingCameraGrant != null) {
            if (grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                pendingCameraGrant.grant(
                        new String[]{PermissionRequest.RESOURCE_VIDEO_CAPTURE});
            } else {
                pendingCameraGrant.deny();
            }
            pendingCameraGrant = null;
        }
        if (requestCode == PICKER_CAMERA_REQ) {
            // camera permission answer for the avatar picker: proceed either
            // way (granted -> gallery+camera chooser, denied -> gallery only)
            launchChooser();
        }
    }

    /**
     * Avatar/file picker. Lazy permission model:
     *  - gallery (ACTION_GET_CONTENT) needs NO runtime permission at all
     *  - camera is requested right here, the moment the user opens the
     *    picker — never at app startup.
     */
    private void openFileChooser(WebChromeClient.FileChooserParams params) {
        pendingChooserParams = params;
        if (checkSelfPermission(Manifest.permission.CAMERA)
                == PackageManager.PERMISSION_GRANTED) {
            launchChooser();
        } else {
            requestPermissions(new String[]{Manifest.permission.CAMERA},
                    PICKER_CAMERA_REQ);
        }
    }

    private void launchChooser() {
        WebChromeClient.FileChooserParams params = pendingChooserParams;
        pendingChooserParams = null;
        if (filePathCallback == null) return;

        Intent contentIntent = (params != null) ? params.createIntent() : null;
        if (contentIntent == null) {
            contentIntent = new Intent(Intent.ACTION_GET_CONTENT);
            contentIntent.setType("image/*");
        }
        contentIntent.addCategory(Intent.CATEGORY_OPENABLE);

        // camera capture option (only when permission is granted)
        Intent cameraIntent = null;
        if (checkSelfPermission(Manifest.permission.CAMERA)
                == PackageManager.PERMISSION_GRANTED) {
            try {
                File captureDir = new File(getCacheDir(), "capture");
                captureDir.mkdirs();
                File img = new File(captureDir,
                        "capture_" + System.currentTimeMillis() + ".jpg");
                cameraCaptureUri = FileProvider.getUriForFile(this,
                        getPackageName() + ".fileprovider", img);
                cameraIntent = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
                cameraIntent.putExtra(MediaStore.EXTRA_OUTPUT, cameraCaptureUri);
                cameraIntent.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
            } catch (Exception e) {
                cameraIntent = null;
            }
        }

        Intent chooser = Intent.createChooser(contentIntent, "选择图片");
        if (cameraIntent != null) {
            chooser.putExtra(Intent.EXTRA_INITIAL_INTENTS,
                    new Intent[]{cameraIntent});
        }
        fileChooserLauncher.launch(chooser);
    }

    /** Read assets/config.bin, deobfuscate, extract "server_url". */
    private String readServerUrl() {
        return stripSlash(extractJsonString(readConfigJson(), "server_url"));
    }

    private String stripSlash(String url) {
        if (url == null || url.isEmpty()) return null;
        while (url.endsWith("/")) url = url.substring(0, url.length() - 1);
        return url;
    }

    private String buildFallbackUrl() {
        String cfg = readConfigRaw();
        String url = "file:///android_asset/web/index.html";
        if (cfg != null && !cfg.isEmpty()) {
            url = url + "#cfg=" + Uri.encode(cfg);
        }
        return url;
    }

    /** Raw obfuscated base64 content of assets/config.bin (or null). */
    private String readConfigRaw() {
        for (String path : new String[]{"config.bin", "web/config.bin"}) {
            try (java.io.InputStream in = getAssets().open(path)) {
                java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
                byte[] buf = new byte[8192];
                int n;
                while ((n = in.read(buf)) != -1) out.write(buf, 0, n);
                String text = new String(out.toByteArray(), "US-ASCII").trim();
                if (!text.isEmpty()) return text;
            } catch (Exception ignored) {
            }
        }
        return null;
    }

    /** Deobfuscated JSON content of config.bin (or null). */
    private String readConfigJson() {
        String raw = readConfigRaw();
        if (raw == null) return null;
        return deobfuscate(raw);
    }

    /** Same algorithm as the web client: base64 decode, XOR with key. */
    private String deobfuscate(String b64) {
        try {
            byte[] raw = Base64.decode(b64.trim(), Base64.DEFAULT);
            byte[] key = OBF_KEY.getBytes("US-ASCII");
            byte[] out = new byte[raw.length];
            for (int i = 0; i < raw.length; i++) {
                out[i] = (byte) (raw[i] ^ key[i % key.length]);
            }
            return new String(out, "UTF-8");
        } catch (Exception e) {
            return null;
        }
    }

    /** Minimal "key": "value" extraction without a JSON dependency. */
    private String extractJsonString(String json, String key) {
        if (json == null) return null;
        String pat = "\"" + key + "\"";
        int i = json.indexOf(pat);
        if (i < 0) return null;
        int colon = json.indexOf(':', i + pat.length());
        if (colon < 0) return null;
        int q1 = json.indexOf('"', colon + 1);
        if (q1 < 0) return null;
        int q2 = json.indexOf('"', q1 + 1);
        if (q2 < 0) return null;
        return json.substring(q1 + 1, q2);
    }

    private void applyThemeBackground() {
        int night = getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK;
        boolean dark = night == Configuration.UI_MODE_NIGHT_YES;
        webView.setBackgroundColor(dark ? 0xFF14161A : 0xFFF0F2F5);
    }

    /** Run JS on the UI thread (used by the updater for progress reports). */
    public void runJs(final String js) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                webView.evaluateJavascript(js, null);
            }
        });
    }

    /**
     * One-time startup diagnostics: POST the device's REAL window-inset
     * values, WebView position, density, startup timing and final URL to
     * the server's /api/client-diag (logged server-side). Lets layout
     * issues on specific ROMs be fixed with measured numbers, not guesses.
     * Fire-and-forget; never blocks the UI.
     */
    private void postDiag(final String pageUrl) {
        final String base = readServerUrl();
        if (base == null) return;
        final String url = base + "/api/client-diag";
        final String body = "{"
                + "\"type\":\"android_boot\","
                + "\"version\":\"" + safeJson(installedVersionName()) + "\","
                + "\"model\":\"" + safeJson(android.os.Build.MODEL) + "\","
                + "\"sdk\":" + android.os.Build.VERSION.SDK_INT + ","
                + "\"density\":" + lastDensity + ","
                + "\"status_bar_px\":" + lastStatusPx + ","
                + "\"cutout_px\":" + lastCutPx + ","
                + "\"cutout_rects\":" + lastRectCount + ","
                + "\"webview_top_px\":" + lastWebViewTopPx + ","
                + "\"pushed_top_dp\":" + measuredTopDp + ","
                + "\"pushed_bottom_dp\":" + measuredBottomDp + ","
                + "\"boot_to_page_ms\":" + (System.currentTimeMillis() - bootMs) + ","
                + "\"fell_back_http\":" + fellBack + ","
                + "\"fell_back_file\":" + fellBackFile + ","
                + "\"url\":\"" + safeJson(pageUrl) + "\""
                + "}";
        new Thread(() -> {
            try {
                java.net.HttpURLConnection c = (java.net.HttpURLConnection)
                        new java.net.URL(url).openConnection();
                c.setConnectTimeout(5000);
                c.setReadTimeout(5000);
                c.setRequestMethod("POST");
                c.setRequestProperty("Content-Type", "application/json");
                c.setDoOutput(true);
                c.getOutputStream().write(body.getBytes("UTF-8"));
                c.getResponseCode();
                c.disconnect();
            } catch (Exception ignored) {
            }
        }).start();
    }

    private String installedVersionName() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Exception e) {
            return "";
        }
    }

    private String safeJson(String s) {
        if (s == null) return "";
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }
}
