#include <windows.h>
#include <wchar.h>
#include <stdio.h>
#include <stdlib.h>

/* XX Down launcher: starts the bundled CPython runtime with the app script.
   Keeps the AV-visible surface minimal: a tiny C exe that spawns the
   official (signed) pythonw.exe.

   It also sends the app's system notifications: the Python app writes a
   signal file (xxdown_toast_<pid>.txt in %TEMP%) when a notification is
   due; a background thread picks it up and posts the balloon from THIS
   process, so the notification identity is "XX Down" (this exe, see the
   VERSIONINFO resource) instead of the Python runtime. */

static volatile LONG g_running = 1;
static HWND g_hwnd = NULL;
static HICON g_hicon = NULL;
static HINSTANCE g_hinst = NULL;

static LRESULT CALLBACK ToastWndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    if (msg == WM_USER + 20) {
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

static int read_toast_file(const wchar_t *file,
                           wchar_t *title, int tlen,
                           wchar_t *msg, int mlen) {
    HANDLE h = CreateFileW(file, GENERIC_READ, FILE_SHARE_DELETE, NULL,
                           OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE)
        return 0;
    char buf[2048];
    DWORD n = 0;
    BOOL ok = ReadFile(h, buf, (DWORD)sizeof(buf) - 1, &n, NULL);
    CloseHandle(h);
    if (!ok || n == 0)
        return 0;
    buf[n] = 0;
    wchar_t wbuf[1024];
    int w = MultiByteToWideChar(CP_UTF8, 0, buf, (int)n, wbuf, 1023);
    if (w <= 0)
        return 0;
    wbuf[w] = 0;
    wchar_t *nl = wcschr(wbuf, L'\n');
    if (nl)
        *nl = 0;
    wcsncpy(title, wbuf, (size_t)tlen - 1);
    title[tlen - 1] = 0;
    if (nl) {
        wcsncpy(msg, nl + 1, (size_t)mlen - 1);
        msg[mlen - 1] = 0;
    }
    return 1;
}

static void post_balloon(const wchar_t *title, const wchar_t *msg) {
    if (!g_hwnd)
        return;
    NOTIFYICONDATAW nid;
    ZeroMemory(&nid, sizeof(nid));
    nid.cbSize = sizeof(nid);
    nid.hWnd = g_hwnd;
    nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP;
    nid.uCallbackMessage = WM_USER + 20;
    nid.hIcon = g_hicon;
    wcsncpy(nid.szTip, L"XX Down", 127);
    Shell_NotifyIconW(NIM_ADD, &nid);
    nid.uFlags = NIF_INFO;
    wcsncpy(nid.szInfoTitle, title, 63);
    wcsncpy(nid.szInfo, msg, 255);
    Shell_NotifyIconW(NIM_MODIFY, &nid);
}

static void remove_tray_icon(void) {
    NOTIFYICONDATAW nid;
    ZeroMemory(&nid, sizeof(nid));
    nid.cbSize = sizeof(nid);
    nid.hWnd = g_hwnd;
    Shell_NotifyIconW(NIM_DELETE, &nid);
}

static DWORD WINAPI toast_thread(LPVOID arg) {
    (void)arg;
    wchar_t file[MAX_PATH];
    wchar_t tmp[MAX_PATH];
    GetTempPathW(MAX_PATH, tmp);
    _snwprintf(file, MAX_PATH, L"%sxxdown_toast_%lu.txt",
               tmp, (unsigned long)GetCurrentProcessId());

    WNDCLASSW wc;
    ZeroMemory(&wc, sizeof(wc));
    wc.lpfnWndProc = ToastWndProc;
    wc.hInstance = g_hinst;
    wc.lpszClassName = L"XXDownToastWnd";
    RegisterClassW(&wc);
    g_hwnd = CreateWindowW(L"XXDownToastWnd", L"XX Down", 0,
                           0, 0, 0, 0, NULL, NULL, g_hinst, NULL);
    g_hicon = (HICON)LoadImageW(g_hinst, MAKEINTRESOURCEW(1),
                                IMAGE_ICON, 0, 0, LR_DEFAULTSIZE);

    while (g_running) {
        if (GetFileAttributesW(file) != INVALID_FILE_ATTRIBUTES) {
            wchar_t title[128];
            wchar_t msg[256];
            title[0] = 0;
            msg[0] = 0;
            if (read_toast_file(file, title, 128, msg, 256)) {
                DeleteFileW(file);
                post_balloon(title, msg);
                Sleep(6000);
                remove_tray_icon();
            }
        }
        Sleep(400);
    }

    if (g_hwnd)
        DestroyWindow(g_hwnd);
    UnregisterClassW(L"XXDownToastWnd", g_hinst);
    return 0;
}

int WINAPI wWinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance,
                    LPWSTR lpCmdLine, int nCmdShow)
{
    (void)hPrevInstance;
    (void)nCmdShow;
    g_hinst = hInstance;

    wchar_t exePath[MAX_PATH];
    if (!GetModuleFileNameW(hInstance, exePath, MAX_PATH))
        return 2;
    wchar_t *slash = wcsrchr(exePath, L'\\');
    if (!slash)
        return 3;
    *slash = L'\0';

    wchar_t cmd[MAX_PATH * 4];
    swprintf(cmd, MAX_PATH * 4,
             L"\"%s\\python\\pythonw.exe\" \"%s\\ytdlp_gui.py\"",
             exePath, exePath);
    if (lpCmdLine && lpCmdLine[0]) {
        wcscat(cmd, L" ");
        wcscat(cmd, lpCmdLine);
    }

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));
    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, 0, NULL, exePath, &si, &pi)) {
        MessageBoxW(NULL,
                    /* ???? pythonw.exe?python ????????\n????? XX Down? */
                    L"\u65E0\u6CD5\u542F\u52A8 pythonw.exe\uFF0Cpython "
                    L"\u8FD0\u884C\u5E93\u53EF\u80FD\u7F3A\u5931\u3002\n"
                    L"\u8BF7\u91CD\u65B0\u5B89\u88C5 XX Down\u3002",
                    L"XX Down", MB_OK | MB_ICONERROR);
        return 1;
    }

    HANDLE th = CreateThread(NULL, 0, toast_thread, NULL, 0, NULL);

    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);

    g_running = 0;
    if (th)
        WaitForSingleObject(th, 3000);
    if (th)
        CloseHandle(th);
    return (int)code;
}
