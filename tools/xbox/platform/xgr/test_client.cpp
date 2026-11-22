// stands in for a GDK title: reaches the runtime the way the static library does (QueryApiImpl + vtable slots)
#include <windows.h>
#include <cstdio>
#include <cstdint>
typedef HRESULT (*Q)(const GUID&, const GUID&, void**);
struct AsyncBlock { void* queue; void* context; void* callback; unsigned char internal[32]; };
static void** Get(Q q, GUID g){ void* o=0; HRESULT hr=q(g,g,&o); if(hr) {printf("query failed %08lx\n",hr); exit(1);} return *(void***)o; }
int main(){
  HMODULE m=LoadLibraryA("xgameruntime.dll"); Q q=(Q)GetProcAddress(m,"QueryApiImpl");
  GUID user={0x01acd177,0x91f9,0x4763,{0xa3,0x8e,0xcc,0xbb,0x55,0xce,0x32,0xe0}};
  GUID thr ={0x073b7dcb,0x1fcf,0x4030,{0x94,0xbe,0xe3,0xc9,0xeb,0x62,0x34,0x28}};
  void* uo=0; q(user,user,&uo); void** uv=*(void***)uo;
  void* to=0; q(thr,thr,&to); void** tv=*(void***)to;
  AsyncBlock ab={}; // no queue: the process default queue is used
  HRESULT hr=((HRESULT(*)(void*,uint32_t,AsyncBlock*))uv[7])(uo,1,&ab);
  printf("XUserAddAsync hr=%08lx\n",hr);
  hr=((HRESULT(*)(void*,AsyncBlock*,BOOLEAN))tv[3])(to,&ab,TRUE);
  printf("XAsyncGetStatus(wait) hr=%08lx\n",hr);
  void* h=0; hr=((HRESULT(*)(void*,AsyncBlock*,void**))uv[8])(uo,&ab,&h);
  printf("XUserAddResult hr=%08lx handle=%p\n",hr,h);
  uint64_t id=0; hr=((HRESULT(*)(void*,void*,uint64_t*))uv[11])(uo,h,&id); printf("XUserGetId hr=%08lx id=%llu\n",hr,id);
  uint32_t st=9; hr=((HRESULT(*)(void*,void*,uint32_t*))uv[14])(uo,h,&st); printf("XUserGetState hr=%08lx state=%u\n",hr,st);
  BYTE has=0; hr=((HRESULT(*)(void*,void*,uint32_t,uint32_t,BYTE*))uv[20])(uo,h,0,1,&has); printf("XUserCheckPrivilege hr=%08lx has=%d\n",hr,has);
  GUID sysg={0xb884675d,0xb738,0x4a9c,{0x81,0x5d,0x9a,0x9a,0x1e,0x0c,0x6c,0x9b}};
  void* so=0; q(sysg,sysg,&so); void** sv=*(void***)so;
  struct Info{ uint16_t os[4],host[4]; char fam[64],form[64]; } info={};
  ((void*(*)(void*,Info*))sv[3])(so,&info); printf("analytics: %s / %s os %u.%u.%u\n",info.fam,info.form,info.os[0],info.os[1],info.os[2]);
  GUID gtg={0xcef4fac0,0x7676,0x4a94,{0xa1,0x19,0x4c,0x43,0xf9,0xeb,0x5b,0x74}};
  void* go=0; q(gtg,gtg,&go); void** gv=*(void***)go;
  for(uint32_t c=0;c<4;c++){ char t[128]={}; size_t used=0; hr=((HRESULT(*)(void*,void*,uint32_t,size_t,char*,size_t*))gv[3])(go,h,c,sizeof(t),t,&used); printf("XUserGetGamertag(%u) hr=%08lx '%s' used=%zu\n",c,hr,t,used); }
  { char t[2]; hr=((HRESULT(*)(void*,void*,uint32_t,size_t,char*,size_t*))gv[3])(go,h,1,sizeof(t),t,nullptr); printf("XUserGetGamertag(short buffer) hr=%08lx\n",hr); }
  GUID plsg={0x41a4e10c,0x5a7e,0x41d9,{0x8c,0x37,0x37,0xbd,0xe6,0x2a,0x07,0xd6}};
  void* po=0; q(plsg,plsg,&po); void** pv=*(void***)po;
  size_t ps=0; hr=((HRESULT(*)(void*,size_t*))pv[3])(po,&ps); printf("XPersistentLocalStorageGetPathSize hr=%08lx size=%zu\n",hr,ps);
  char path[MAX_PATH]={}; size_t pu=0; hr=((HRESULT(*)(void*,size_t,char*,size_t*))pv[4])(po,sizeof(path),path,&pu); printf("XPersistentLocalStorageGetPath hr=%08lx '%s' used=%zu exists=%d\n",hr,path,pu,GetFileAttributesA(path)!=INVALID_FILE_ATTRIBUTES);
  hr=((HRESULT(*)(void*,size_t,char*,size_t*))pv[4])(po,ps-1,path,&pu); printf("XPersistentLocalStorageGetPath(short buffer) hr=%08lx\n",hr);
  struct Space{ uint64_t avail,totalFree,used,total; } sp={};
  hr=((HRESULT(*)(void*,Space*))pv[5])(po,&sp); printf("XPersistentLocalStorageGetSpaceInfo hr=%08lx avail=%llu totalFree=%llu used=%llu total=%llu\n",hr,sp.avail,sp.totalFree,sp.used,sp.total);
  { AsyncBlock pb={}; hr=((HRESULT(*)(void*,uint64_t,AsyncBlock*))pv[6])(po,1<<20,&pb); printf("XPersistentLocalStoragePromptUserForSpaceAsync hr=%08lx\n",hr);
    hr=((HRESULT(*)(void*,AsyncBlock*,BOOLEAN))tv[3])(to,&pb,TRUE); printf("XAsyncGetStatus(wait) hr=%08lx (E_ABORT=%08lx)\n",hr,(unsigned long)E_ABORT);
    hr=((HRESULT(*)(void*,AsyncBlock*))pv[7])(po,&pb); printf("XPersistentLocalStoragePromptUserForSpaceResult hr=%08lx (E_ABORT=%08lx)\n",hr,(unsigned long)E_ABORT);
    hr=((HRESULT(*)(void*,uint64_t,AsyncBlock*))pv[6])(po,1<<20,nullptr); printf("XPersistentLocalStoragePromptUserForSpaceAsync(null block) hr=%08lx\n",hr);
    hr=((HRESULT(*)(void*,AsyncBlock*))pv[7])(po,nullptr); printf("XPersistentLocalStoragePromptUserForSpaceResult(null block) hr=%08lx\n",hr); }
  GUID pkg={0x3720de07,0xe8e4,0x44a3,{0xad,0x32,0xb3,0x59,0xe8,0xad,0xbe,0x55}};
  void* ko=0; q(pkg,pkg,&ko); void** kv=*(void***)ko;
  printf("XPackageIsPackagedProcess=%d\n",(int)((BYTE(*)(void*))kv[4])(ko));
  char loc[LOCALE_NAME_MAX_LENGTH]={}; hr=((HRESULT(*)(void*,size_t,char*))kv[11])(ko,sizeof(loc),loc); printf("XPackageGetUserLocale hr=%08lx '%s'\n",hr,loc);
  hr=((HRESULT(*)(void*,size_t,char*))kv[11])(ko,2,loc); printf("XPackageGetUserLocale(short buffer) hr=%08lx\n",hr);
  ((void(*)(void*,void*))uv[4])(uo,h);
  return 0;
}
