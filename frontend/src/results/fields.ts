import type { Json, Result } from './types'

export type FieldValue = { found: true; value: Json } | { found: false }
/** Read an RFC 6901 pointer without traversing prototypes, accessors or array properties. */
export function resolveResultPointer(document: Json, pointer: string): FieldValue {
  if (pointer.length > 4096 || pointer !== '' && !pointer.startsWith('/') || /~(?![01])/.test(pointer)) return { found:false }
  if (pointer === '') return { found:true, value:document }
  const segments = pointer.slice(1).split('/')
  if (segments.length > 24) return { found:false }
  let current = document
  for (const escaped of segments) {
    const key = escaped.replace(/~1/g,'/').replace(/~0/g,'~')
    if (['__proto__','prototype','constructor'].includes(key) || current === null || typeof current !== 'object') return { found:false }
    if (Array.isArray(current) && (!/^(0|[1-9][0-9]*)$/.test(key) || !Number.isSafeInteger(Number(key)) || Number(key) >= current.length)) return { found:false }
    const property = Object.getOwnPropertyDescriptor(current,key)
    if (!property || !Object.hasOwn(property,'value')) return { found:false }
    current = property.value as Json
  }
  return { found:true, value:current }
}
export function resultFieldValue(result: Result, path: string): FieldValue {
  // Runtime coverage checks use this reserved prefix outside the scenario items.
  if (path === '/coverage' || path.startsWith('/coverage/')) return resolveResultPointer(result.coverage as unknown as Json,path.slice('/coverage'.length))
  return resolveResultPointer(result.items,path)
}
export function checkExplanation(actual: Json): string {
  const code = actual && typeof actual === 'object' && !Array.isArray(actual) && typeof actual.code === 'string' ? actual.code : ''
  return ({ original_value_matches:'结果值与对应证据一致。', original_value_mismatch:'结果值与对应证据不一致。', original_values_conflict:'对应证据中的值存在冲突。', original_field_unavailable:'无法从对应证据中读取并核实此字段。', observed_read_count_outside_limit:'实际读取数量超出任务允许范围。' }[code] ?? '检查器已保存此字段的核查记录，可展开查看详情。')
}
