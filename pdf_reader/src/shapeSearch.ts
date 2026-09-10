/**
 * Small PDF path parser used by the vector playground preview.
 * Runtime matching is handled by pdf_parser/tools/vector_matcher.py.
 */

export type PathCommand = {
  op: string
  points?: number[][]
}

type PathPolyline = {
  points: [number, number][]
  closed: boolean
}

function isNumericToken(t: string): boolean {
  return /^-?(?:\d+\.?\d*|\.\d+)$/.test(t)
}

/** Tokenize a PDF content fragment (numbers and operators). */
export function tokenizePdfContentStream(src: string): string[] {
  const out: string[] = []
  let i = 0
  while (i < src.length) {
    const ch = src[i]
    if (ch === '%') {
      while (i < src.length && src[i] !== '\n' && src[i] !== '\r') {
        i++
      }
      continue
    }
    if (/\s/.test(ch)) {
      i++
      continue
    }
    const rest = src.slice(i)
    const num = rest.match(/^-?(?:\d+\.?\d*|\.\d+)/)
    if (num) {
      out.push(num[0])
      i += num[0].length
      continue
    }
    const op = rest.match(/^[A-Za-z*']+/)
    if (op) {
      out.push(op[0])
      i += op[0].length
      continue
    }
    i++
  }
  return out
}

/** Parse path-related operators into commands (user-space, identity CTM). */
export function parsePathCommandsFromTokens(tokens: string[]): PathCommand[] {
  const cmds: PathCommand[] = []
  const stack: number[] = []
  let current: [number, number] | null = null

  const popN = (n: number): number[] | null => {
    if (stack.length < n) {
      return null
    }
    return stack.splice(stack.length - n, n)
  }

  for (const t of tokens) {
    if (isNumericToken(t)) {
      stack.push(Number.parseFloat(t))
      continue
    }

    switch (t) {
      case 'm': {
        const p = popN(2)
        if (p) {
          cmds.push({ op: 'M', points: [[p[0], p[1]]] })
          current = [p[0], p[1]]
        }
        break
      }
      case 'l': {
        const p = popN(2)
        if (p) {
          cmds.push({ op: 'L', points: [[p[0], p[1]]] })
          current = [p[0], p[1]]
        }
        break
      }
      case 'c': {
        const p = popN(6)
        if (p) {
          cmds.push({
            op: 'C',
            points: [
              [p[0], p[1]],
              [p[2], p[3]],
              [p[4], p[5]],
            ],
          })
          current = [p[4], p[5]]
        }
        break
      }
      case 'v': {
        const p = popN(4)
        if (p && current) {
          cmds.push({
            op: 'C',
            points: [current, [p[0], p[1]], [p[2], p[3]]],
          })
          current = [p[2], p[3]]
        }
        break
      }
      case 'y': {
        const p = popN(4)
        if (p && current) {
          const end: [number, number] = [p[2], p[3]]
          cmds.push({
            op: 'C',
            points: [[p[0], p[1]], end, end],
          })
          current = end
        }
        break
      }
      case 're': {
        const p = popN(4)
        if (p) {
          const [x, y, w, h] = p
          cmds.push({ op: 'M', points: [[x, y]] })
          cmds.push({ op: 'L', points: [[x + w, y]] })
          cmds.push({ op: 'L', points: [[x + w, y + h]] })
          cmds.push({ op: 'L', points: [[x, y + h]] })
          cmds.push({ op: 'Z' })
          current = [x, y]
        }
        break
      }
      case 'h':
        cmds.push({ op: 'Z' })
        break
      default:
        break
    }
  }

  return cmds
}

export function parseSnippetToPathCommands(snippet: string): PathCommand[] {
  return parsePathCommandsFromTokens(tokenizePdfContentStream(snippet))
}

function bezierPoint(
  p0: [number, number],
  p1: [number, number],
  p2: [number, number],
  p3: [number, number],
  t: number,
): [number, number] {
  const u = 1 - t
  const tt = t * t
  const uu = u * u
  const uuu = uu * u
  const ttt = tt * t
  const x = uuu * p0[0] + 3 * uu * t * p1[0] + 3 * u * tt * p2[0] + ttt * p3[0]
  const y = uuu * p0[1] + 3 * uu * t * p1[1] + 3 * u * tt * p2[1] + ttt * p3[1]
  return [x, y]
}

/** Split commands into subpaths and flatten each subpath to a polyline. */
export function commandsToPolylines(commands: PathCommand[]): PathPolyline[] {
  const out: PathPolyline[] = []
  let points: [number, number][] = []
  let current: [number, number] | null = null
  let subStart: [number, number] | null = null
  let closed = false

  const flush = () => {
    if (points.length >= 2) {
      out.push({ points: points.slice(), closed })
    }
    points = []
    current = null
    subStart = null
    closed = false
  }

  for (const c of commands) {
    if (c.op === 'M' && c.points?.[0]) {
      flush()
      const p = c.points[0] as [number, number]
      points.push(p)
      current = p
      subStart = p
      continue
    }

    if (c.op === 'L' && c.points?.[0]) {
      const p = c.points[0] as [number, number]
      if (!current) {
        points.push(p)
      } else {
        points.push(p)
      }
      current = p
      if (!subStart) {
        subStart = points[0] ?? p
      }
      continue
    }

    if (c.op === 'C' && c.points?.length === 3 && current) {
      const [p1, p2, p3] = c.points as [[number, number], [number, number], [number, number]]
      const p0 = current
      for (let s = 1; s <= 6; s++) {
        const t = s / 6
        points.push(bezierPoint(p0, p1, p2, p3, t))
      }
      current = p3
      continue
    }

    if (c.op === 'Z' && subStart && current) {
      points.push(subStart)
      current = subStart
      closed = true
    }
  }

  flush()
  return out
}
