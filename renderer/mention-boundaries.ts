const segmenter = new Intl.Segmenter('zh-CN', { granularity: 'word' });

export function mentionContext(text: string, start = 0, end = text.length) {
  const boundaries = new Set<number>([0, text.length]);
  for (const segment of segmenter.segment(text)) {
    boundaries.add(segment.index);
    boundaries.add(segment.index + segment.segment.length);
  }
  return { text, start, end, boundaries };
}

type MentionContext = ReturnType<typeof mentionContext>;

export function wholeWordOccurrences(context: MentionContext, term: string) {
  const occurrences: { start: number; end: number }[] = [];
  if (!term) return occurrences;
  const pattern = new RegExp(term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'giu');
  for (const match of context.text.matchAll(pattern)) {
    const start = match.index, end = start + match[0].length;
    if (start >= context.start && end <= context.end && context.boundaries.has(start) && context.boundaries.has(end)) {
      occurrences.push({ start, end });
    }
  }
  return occurrences;
}

export function matchMentionTerm(context: MentionContext, rawTerm: string) {
  const term = rawTerm.trim();
  if (!term) return null;
  const exact = wholeWordOccurrences(context, term)[0];
  if (exact) return context.text.slice(exact.start, exact.end);
  const characters = [...term.replace(/[\s·・—_\-:：,，。;；/\\()（）\[\]【】]+/g, '')];
  if (characters.length < 2) return null;
  const gap = '[\\s·・—_\\-:：,，。;；/\\\\()（）\\[\\]【】]*';
  const pattern = characters.map(character => character.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join(gap);
  for (const match of context.text.matchAll(new RegExp(pattern, 'giu'))) {
    const start = match.index, end = start + match[0].length;
    if (start >= context.start && end <= context.end && context.boundaries.has(start) && context.boundaries.has(end)) return match[0];
  }
  return null;
}
