target datalayout = "e-m:e-p:32:32-i1:32-i8:8-i16:16-i32:32-i64:64-f16:16-f32:32-f64:64-n8:16:32:64"
target triple = "dxil-ms-xb2"

%dx.types.Handle = type { i8* }
%dx.types.ResRet.i32 = type { i32, i32, i32, i32, i32 }
%dx.types.ResRet.i64 = type { i64, i64, i64, i64, i32 }
%struct.RWByteAddressBuffer = type { i32 }

define void @main() {
  %1 = call %dx.types.Handle @dx.op.createHandle(i32 57, i8 1, i32 0, i32 0, i1 false)
  %2 = call %dx.types.Handle @dx.op.createHandle(i32 57, i8 1, i32 1, i32 1, i1 false)
  %3 = call i32 @dx.op.groupId.i32(i32 94, i32 0)
  %4 = call i32 @dx.op.flattenedThreadIdInGroup.i32(i32 96)
  %5 = shl i32 %3, 6
  %6 = add i32 %5, %4
  %7 = shl i32 %6, 4
  %8 = call %dx.types.ResRet.i64 @dx.op.rawBufferLoad.i64(i32 139, %dx.types.Handle %1, i32 %7, i32 undef, i8 3, i32 8)
  %9 = extractvalue %dx.types.ResRet.i64 %8, 0
  %10 = add i32 %7, 8
  %11 = call %dx.types.ResRet.i64 @dx.op.rawBufferLoad.i64(i32 139, %dx.types.Handle %1, i32 %10, i32 undef, i8 3, i32 8)
  %12 = extractvalue %dx.types.ResRet.i64 %11, 0
  %13 = call i32 @xbox.global_load_dword_with_cache(i32 356, i64 %9, i32 undef, i32 undef)
  %14 = call { i32, i32, i32, i32 } @xbox.global_load_dwordx4_with_cache(i32 359, i64 %9, i32 undef, i32 undef)
  %15 = call i32 @xbox.s_load_dword_with_cache(i32 24, i64 %9, i32 undef, i32 undef)
  call void @xbox.global_store_dword_with_cache(i32 362, i64 %12, i32 -559038737, i32 undef, i32 undef)
  %16 = call i32 @xbox.global_atomic_add(i32 366, i64 %12, i32 7)
  %17 = call i32 @xbox.global_atomic_sub(i32 367, i64 %12, i32 5)
  %18 = extractvalue { i32, i32, i32, i32 } %14, 0
  %19 = extractvalue { i32, i32, i32, i32 } %14, 1
  %20 = extractvalue { i32, i32, i32, i32 } %14, 2
  %21 = extractvalue { i32, i32, i32, i32 } %14, 3
  %22 = shl i32 %6, 5
  call void @dx.op.rawBufferStore.i32(i32 140, %dx.types.Handle %2, i32 %22, i32 undef, i32 %13, i32 undef, i32 undef, i32 undef, i8 1, i32 4)
  %23 = add i32 %22, 4
  call void @dx.op.rawBufferStore.i32(i32 140, %dx.types.Handle %2, i32 %23, i32 undef, i32 %15, i32 undef, i32 undef, i32 undef, i8 1, i32 4)
  %24 = add i32 %22, 8
  call void @dx.op.rawBufferStore.i32(i32 140, %dx.types.Handle %2, i32 %24, i32 undef, i32 %16, i32 undef, i32 undef, i32 undef, i8 1, i32 4)
  %25 = add i32 %22, 12
  call void @dx.op.rawBufferStore.i32(i32 140, %dx.types.Handle %2, i32 %25, i32 undef, i32 %17, i32 undef, i32 undef, i32 undef, i8 1, i32 4)
  %26 = add i32 %22, 16
  call void @dx.op.rawBufferStore.i32(i32 140, %dx.types.Handle %2, i32 %26, i32 undef, i32 %18, i32 %19, i32 %20, i32 %21, i8 15, i32 4)
  ret void
}

declare i32 @xbox.global_load_dword_with_cache(i32, i64, i32, i32) #1
declare { i32, i32, i32, i32 } @xbox.global_load_dwordx4_with_cache(i32, i64, i32, i32) #1
declare i32 @xbox.s_load_dword_with_cache(i32, i64, i32, i32) #1
declare void @xbox.global_store_dword_with_cache(i32, i64, i32, i32, i32) #1
declare i32 @xbox.global_atomic_add(i32, i64, i32) #1
declare i32 @xbox.global_atomic_sub(i32, i64, i32) #1
declare i32 @dx.op.groupId.i32(i32, i32) #0
declare i32 @dx.op.flattenedThreadIdInGroup.i32(i32) #0
declare %dx.types.ResRet.i32 @dx.op.rawBufferLoad.i32(i32, %dx.types.Handle, i32, i32, i8, i32) #2
declare %dx.types.ResRet.i64 @dx.op.rawBufferLoad.i64(i32, %dx.types.Handle, i32, i32, i8, i32) #2
declare void @dx.op.rawBufferStore.i32(i32, %dx.types.Handle, i32, i32, i32, i32, i32, i32, i8, i32) #1
declare %dx.types.Handle @dx.op.createHandle(i32, i8, i32, i32, i1) #2
attributes #0 = { nounwind readnone }
attributes #1 = { nounwind }
attributes #2 = { nounwind readonly }
!llvm.ident = !{!0}
!dx.version = !{!1}
!dx.valver = !{!2}
!dx.shaderModel = !{!3}
!dx.resources = !{!4}
!dx.entryPoints = !{!10}
!0 = !{!"dxcxbox 2504.2509.11601.10011"}
!1 = !{i32 1, i32 5}
!2 = !{i32 1, i32 8}
!3 = !{!"cs", i32 6, i32 5}
!4 = !{null, !5, null, null}
!5 = !{!6, !7}
!6 = !{i32 0, %struct.RWByteAddressBuffer* undef, !"", i32 0, i32 0, i32 1, i32 11, i1 false, i1 false, i1 false, null}
!7 = !{i32 1, %struct.RWByteAddressBuffer* undef, !"", i32 0, i32 1, i32 1, i32 11, i1 false, i1 false, i1 false, null}
!10 = !{void ()* @main, !"main", null, !4, !11}
!11 = !{i32 0, i64 48, i32 4, !12}
!12 = !{i32 64, i32 1, i32 1}
