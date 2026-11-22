#define GTX_SHIM_DLL "xgameruntime.dll"
#include <windows.h>

#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <string>

#include "gtx/log.h"
#include "xgr_internal.h"
#include "xtask.h"

namespace gtx::xgr::storage {
namespace {

struct SpaceInfo {
  uint64_t availableFreeBytes;
  uint64_t totalFreeBytes;
  uint64_t usedBytes;
  uint64_t totalBytes;
};

std::string exe_dir() {
  char p[MAX_PATH * 4];
  DWORD n = GetModuleFileNameA(nullptr, p, sizeof(p));
  if (!n || n >= sizeof(p)) return {};
  std::string s(p, n);
  size_t cut = s.find_last_of("\\/");
  return cut == std::string::npos ? std::string() : s.substr(0, cut + 1);
}

uint64_t config_quota_mb() {
  std::string path = exe_dir() + "MicrosoftGame.Config";
  HANDLE f = CreateFileA(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
  if (f == INVALID_HANDLE_VALUE) return 0;
  std::string text;
  char buf[4096];
  DWORD got = 0;
  while (ReadFile(f, buf, sizeof(buf), &got, nullptr) && got) text.append(buf, got);
  CloseHandle(f);
  size_t sec = text.find("<PersistentLocalStorage>");
  if (sec == std::string::npos) return 0;
  size_t end = text.find("</PersistentLocalStorage>", sec);
  size_t tag = text.find("<SizeMB>", sec);
  if (tag == std::string::npos || (end != std::string::npos && tag > end)) return 0;
  return std::strtoull(text.c_str() + tag + 8, nullptr, 10);
}

uint64_t quota_bytes() {
  static const uint64_t q = [] {
    const char* e = std::getenv("XGR_PLS_SIZE_MB");
    uint64_t mb = e ? std::strtoull(e, nullptr, 10) : config_quota_mb();
    return mb * 1024ull * 1024ull;
  }();
  return q;
}

bool make_dirs(const std::string& dir) {
  for (size_t i = 0; i < dir.size(); ++i) {
    if ((dir[i] == '\\' || dir[i] == '/') && i > 0 && dir[i - 1] != ':') {
      std::string part = dir.substr(0, i);
      if (!CreateDirectoryA(part.c_str(), nullptr) && GetLastError() != ERROR_ALREADY_EXISTS) return false;
    }
  }
  return true;
}

const std::string& root() {
  static const std::string r = [] {
    const char* e = std::getenv("XGR_PLS_DIR");
    std::string d = e && *e ? std::string(e) : exe_dir() + "PersistentLocalStorage";
    if (d.empty()) return std::string();
    if (d.back() != '\\' && d.back() != '/') d += '\\';
    return make_dirs(d) ? d : std::string();
  }();
  return r;
}

HRESULT ready(const char* fn) {
  if (!quota_bytes()) {
    gtx::log::write(gtx::log::Kind::Warn, GTX_SHIM_DLL, fn, nullptr, "no PersistentLocalStorage quota (MicrosoftGame.Config SizeMB or XGR_PLS_SIZE_MB)");
    return E_NOTIMPL;
  }
  if (root().empty()) return HRESULT_FROM_WIN32(ERROR_PATH_NOT_FOUND);
  return S_OK;
}

uint64_t tree_size(const std::string& dir) {
  uint64_t total = 0;
  WIN32_FIND_DATAA fd;
  HANDLE h = FindFirstFileA((dir + "*").c_str(), &fd);
  if (h == INVALID_HANDLE_VALUE) return 0;
  do {
    if (!std::strcmp(fd.cFileName, ".") || !std::strcmp(fd.cFileName, "..")) continue;
    if (fd.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) continue;
    if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) total += tree_size(dir + fd.cFileName + "\\");
    else total += (uint64_t(fd.nFileSizeHigh) << 32) | fd.nFileSizeLow;
  } while (FindNextFileA(h, &fd));
  FindClose(h);
  return total;
}

HRESULT XPersistentLocalStorageGetPathSize_impl(void*, size_t* size) {
  if (!size) return E_INVALIDARG;
  if (HRESULT hr = ready("XPersistentLocalStorageGetPathSize"); FAILED(hr)) return hr;
  *size = root().size() + 1;
  return S_OK;
}

HRESULT XPersistentLocalStorageGetPath_impl(void*, size_t size, char* path, size_t* used) {
  if (!path) return E_INVALIDARG;
  if (HRESULT hr = ready("XPersistentLocalStorageGetPath"); FAILED(hr)) return hr;
  const std::string& r = root();
  if (size < r.size() + 1) return HRESULT_FROM_WIN32(ERROR_INSUFFICIENT_BUFFER);
  std::memcpy(path, r.c_str(), r.size() + 1);
  if (used) *used = r.size() + 1;
  return S_OK;
}

HRESULT XPersistentLocalStorageGetSpaceInfo_impl(void*, SpaceInfo* info) {
  if (!info) return E_INVALIDARG;
  if (HRESULT hr = ready("XPersistentLocalStorageGetSpaceInfo"); FAILED(hr)) return hr;
  uint64_t total = quota_bytes(), used = tree_size(root());
  uint64_t free = used < total ? total - used : 0;
  ULARGE_INTEGER avail = {};
  if (GetDiskFreeSpaceExA(root().c_str(), &avail, nullptr, nullptr) && avail.QuadPart < free) free = avail.QuadPart;
  info->totalBytes = total;
  info->usedBytes = used;
  info->totalFreeBytes = used < total ? total - used : 0;
  info->availableFreeBytes = free;
  return S_OK;
}

HRESULT CALLBACK no_prompt_provider(task::AsyncOp op, const task::AsyncProviderData*) {
  return op == task::AsyncOp::DoWork ? E_ABORT : S_OK;
}

HRESULT XPersistentLocalStoragePromptUserForSpaceAsync_impl(void*, uint64_t, task::AsyncBlock* async) {
  if (!async) return E_INVALIDARG;
  HRESULT hr = task::AsyncBegin(async, nullptr, reinterpret_cast<const void*>(&XPersistentLocalStoragePromptUserForSpaceAsync_impl),
                                "XPersistentLocalStoragePromptUserForSpaceAsync", no_prompt_provider);
  if (FAILED(hr)) return hr;
  return task::AsyncSchedule(async, 0);
}

HRESULT XPersistentLocalStoragePromptUserForSpaceResult_impl(void*, task::AsyncBlock* async) {
  if (!async) return E_INVALIDARG;
  return task::AsyncGetResult(async, reinterpret_cast<const void*>(&XPersistentLocalStoragePromptUserForSpaceAsync_impl), 0, nullptr, nullptr);
}

const Override kOverrides[] = {
    {"XPersistentLocalStorageGetPathSize", reinterpret_cast<void*>(&XPersistentLocalStorageGetPathSize_impl)},
    {"XPersistentLocalStorageGetPath", reinterpret_cast<void*>(&XPersistentLocalStorageGetPath_impl)},
    {"XPersistentLocalStorageGetSpaceInfo", reinterpret_cast<void*>(&XPersistentLocalStorageGetSpaceInfo_impl)},
    {"XPersistentLocalStoragePromptUserForSpaceAsync", reinterpret_cast<void*>(&XPersistentLocalStoragePromptUserForSpaceAsync_impl)},
    {"XPersistentLocalStoragePromptUserForSpaceResult", reinterpret_cast<void*>(&XPersistentLocalStoragePromptUserForSpaceResult_impl)},
};

}  // namespace

const Override* overrides(size_t* count) {
  *count = sizeof(kOverrides) / sizeof(kOverrides[0]);
  return kOverrides;
}

}  // namespace gtx::xgr::storage
