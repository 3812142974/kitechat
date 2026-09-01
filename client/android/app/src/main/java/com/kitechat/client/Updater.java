package com.kitechat.client;

import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;

import androidx.core.content.FileProvider;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.URL;
import java.net.URLConnection;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * In-app self-update.
 *
 * On every launch the web layer asks KCNative.scanUpdates(), which:
 *   - deletes leftover APKs whose version equals the installed one
 *   - returns info about any APK whose version is NEWER
 * If the user accepts, KCNative.downloadAndInstall(url) streams the new
 * APK to external-files/downloads and fires the system installer intent.
 */
public class Updater {

    public static final AtomicBoolean DOWNLOADING = new AtomicBoolean(false);

    /** JSON array (hand-built string) describing local APK candidates. */
    public static String scanLocalApks(Context ctx) {
        StringBuilder sb = new StringBuilder("[");
        try {
            File dir = new File(ctx.getExternalFilesDir(null), "downloads");
            File[] files = (dir != null && dir.isDirectory())
                    ? dir.listFiles() : null;
            String installed = installedVersion(ctx);
            if (files != null) {
                for (File f : files) {
                    String name = f.getName();
                    if (!name.toLowerCase().endsWith(".apk")) continue;
                    String ver = versionFromName(name);
                    // same version as installed -> stale leftover, delete
                    if (ver != null && ver.equals(installed)) {
                        f.delete();
                        continue;
                    }
                    if (sb.length() > 1) sb.append(',');
                    sb.append("{\"version\":\"").append(esc(ver == null ? "" : ver))
                      .append("\",\"path\":\"").append(esc(f.getAbsolutePath()))
                      .append("\",\"size\":").append(f.length())
                      .append('}');
                }
            }
        } catch (Throwable ignored) {
        }
        sb.append(']');
        return sb.toString();
    }

    /** Extract version from KiteChat-<version>-<ts>.apk. */
    static String versionFromName(String name) {
        String base = name.substring(0, name.length() - 4); // strip .apk
        String prefix = "KiteChat-";
        if (!base.startsWith(prefix)) return null;
        String rest = base.substring(prefix.length());
        int dash = rest.lastIndexOf('-');
        if (dash <= 0) return null;
        String tail = rest.substring(dash + 1);
        // tail must be the 14-digit timestamp
        if (tail.length() == 14 && tail.matches("\\d+")) {
            return rest.substring(0, dash);
        }
        return rest;
    }

    static String installedVersion(Context ctx) {
        try {
            if (Build.VERSION.SDK_INT >= 28) {
                return ctx.getPackageManager().getPackageInfo(
                        ctx.getPackageName(), 0).getLongVersionCode()
                        + ":" + ctx.getPackageManager().getPackageInfo(
                        ctx.getPackageName(), 0).versionName;
            }
            return ctx.getPackageManager().getPackageInfo(
                    ctx.getPackageName(), 0).versionName;
        } catch (PackageManager.NameNotFoundException e) {
            return null;
        }
    }

    /**
     * Stream url -> downloads/<filename>, then launch installer.
     * Progress is reported back to the page via window.KCUpdateProgress.
     */
    public static void downloadAndInstall(final Context ctx,
            final String url, final String filename) {
        if (!DOWNLOADING.compareAndSet(false, true)) return;
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    File dir = new File(ctx.getExternalFilesDir(null),
                            "downloads");
                    if (!dir.exists()) dir.mkdirs();
                    File dst = new File(dir, filename);
                    if (dst.exists()) dst.delete();

                    URL u = new URL(url);
                    URLConnection conn = u.openConnection();
                    conn.setConnectTimeout(15000);
                    conn.setReadTimeout(60000);
                    long total = conn.getContentLengthLong();
                    InputStream in = conn.getInputStream();
                    OutputStream out = new FileOutputStream(dst);
                    byte[] buf = new byte[16384];
                    long done = 0;
                    int n;
                    int lastPct = -1;
                    while ((n = in.read(buf)) != -1) {
                        out.write(buf, 0, n);
                        done += n;
                        int pct = total > 0 ? (int) (done * 100 / total) : -1;
                        if (pct != lastPct) {
                            lastPct = pct;
                            postProgress(ctx, pct, done, total);
                        }
                    }
                    out.close();
                    in.close();
                    postProgress(ctx, 100, done, total);
                    // small delay so the UI shows 100%
                    Thread.sleep(300);
                    launchInstaller(ctx, dst);
                } catch (Exception e) {
                    postError(ctx, e.getMessage() == null
                            ? e.getClass().getSimpleName() : e.getMessage());
                } finally {
                    DOWNLOADING.set(false);
                }
            }
        }).start();
    }

    private static void launchInstaller(Context ctx, File apk) {
        launchInstallerLocal(ctx, apk);
    }

    /** Public entry: launch the system installer for an existing APK. */
    public static void launchInstallerLocal(Context ctx, File apk) {
        Uri uri;
        Intent intent = new Intent(Intent.ACTION_VIEW);
        if (Build.VERSION.SDK_INT >= 24) {
            uri = FileProvider.getUriForFile(ctx,
                    ctx.getPackageName() + ".fileprovider", apk);
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        } else {
            uri = Uri.fromFile(apk);
        }
        intent.setDataAndType(uri,
                "application/vnd.android.package-archive");
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        ctx.startActivity(intent);
    }

    private static void postProgress(final Context ctx, final int pct,
            final long done, final long total) {
        new android.os.Handler(android.os.Looper.getMainLooper()).post(
                new Runnable() {
            @Override
            public void run() {
                if (ctx instanceof MainActivity) {
                    ((MainActivity) ctx).runJs(
                            "window.KCUpdateProgress && KCUpdateProgress("
                                    + pct + "," + done + "," + total + ")");
                }
            }
        });
    }

    private static void postError(final Context ctx, final String msg) {
        new android.os.Handler(android.os.Looper.getMainLooper()).post(
                new Runnable() {
            @Override
            public void run() {
                if (ctx instanceof MainActivity) {
                    ((MainActivity) ctx).runJs(
                            "window.KCUpdateError && KCUpdateError(\""
                                    + esc(msg) + "\")");
                }
            }
        });
    }

    private static String esc(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }
}
