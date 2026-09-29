// Finds ```mermaid fenced blocks in a model's output. Pure, so it can be
// tested with `node --test` and no browser.
//
// Fences follow CommonMark: one opens on its own line with up to three spaces
// of indent and three or more backticks or tildes, and closes on a line of the
// same character, at least as long, with nothing else on it. Every fence is
// tracked, so a ```mermaid line quoted inside some other code block is left
// alone; only blocks whose info string is `mermaid` are returned. A block the
// output never closes (an answer cut off at the token cap) runs to the end, as
// in CommonMark, and is marked `unclosed` so the panel can say so.

const OPEN = /^ {0,3}(`{3,}|~{3,})(.*)$/
const CLOSE = /^ {0,3}(`{3,}|~{3,})[ \t]*$/

export function extractMermaidBlocks(text) {
  if (!text || !/mermaid/i.test(text)) return []
  const blocks = []
  let open = null

  for (const line of text.split(/\r?\n/)) {
    if (!open) {
      const match = OPEN.exec(line)
      // A backtick fence's info string may not itself contain a backtick.
      if (match && !(match[1][0] === '`' && match[2].includes('`'))) {
        const info = match[2].trim().split(/\s+/)[0].toLowerCase()
        open = { fence: match[1], mermaid: info === 'mermaid', body: [] }
      }
      continue
    }
    const close = CLOSE.exec(line)
    if (close && close[1][0] === open.fence[0] && close[1].length >= open.fence.length) {
      if (open.mermaid) blocks.push({ code: open.body.join('\n'), unclosed: false })
      open = null
    } else {
      open.body.push(line)
    }
  }
  if (open?.mermaid) blocks.push({ code: open.body.join('\n'), unclosed: true })
  return blocks
}
