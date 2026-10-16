import { Fragment } from "react";

type LinkifiedTextProps = {
  text: string;
};

type TextPart =
  | { kind: "text"; value: string }
  | { kind: "url"; value: string };

const HTTP_URL_PATTERN = /https?:\/\/[^\s<>"'`，。；！？、）】“”‘’《》〈〉「」『』]+/giu;
const TRAILING_PUNCTUATION = /[.,!?;:，。！？；：、`”’》〉」』]/u;
const URL_BRACKET_PAIRS = [["(", ")"], ["[", "]"], ["{", "}"]] as const;

/**
 * 把 Console 公共文本中的 HTTP(S) 地址展示为安全的新窗口链接。
 *
 * Args:
 *   props: 需要识别链接的原始正文。
 *
 * Returns:
 *   保留完整点击目标且缩短视觉文案的行内内容。
 */
export function LinkifiedText({ text }: LinkifiedTextProps): JSX.Element {
  return (
    <>
      {splitText(text).map((part, index) => part.kind === "text" ? (
        <Fragment key={`text-${index}`}>{part.value}</Fragment>
      ) : (
        <a
          className="underline underline-offset-2"
          href={part.value}
          key={`url-${index}`}
          rel="noopener noreferrer"
          target="_blank"
          title={part.value}
        >
          {shortenUrl(part.value)}
        </a>
      ))}
    </>
  );
}

function splitText(text: string): TextPart[] {
  const parts: TextPart[] = [];
  let cursor = 0;
  for (const match of text.matchAll(HTTP_URL_PATTERN)) {
    const matchIndex = match.index ?? cursor;
    const url = trimUrlPunctuation(match[0]);
    if (!url) {
      continue;
    }
    if (matchIndex > cursor) {
      parts.push({ kind: "text", value: text.slice(cursor, matchIndex) });
    }
    parts.push({ kind: "url", value: url });
    cursor = matchIndex + url.length;
  }
  if (cursor < text.length) {
    parts.push({ kind: "text", value: text.slice(cursor) });
  }
  return parts;
}

function trimUrlPunctuation(value: string): string {
  let end = value.length;
  while (end > 0 && TRAILING_PUNCTUATION.test(value[end - 1] ?? "")) {
    end -= 1;
  }
  let candidate = value.slice(0, end);
  let changed = true;
  while (candidate && changed) {
    changed = false;
    for (const [opening, closing] of URL_BRACKET_PAIRS) {
      if (
        candidate.endsWith(closing) &&
        countCharacter(candidate, closing) > countCharacter(candidate, opening)
      ) {
        candidate = candidate.slice(0, -1);
        changed = true;
        break;
      }
    }
  }
  return candidate;
}

function countCharacter(value: string, target: string): number {
  return [...value].filter(character => character === target).length;
}

function shortenUrl(url: string): string {
  if (url.length <= 52) {
    return url;
  }
  return `${url.slice(0, 34)}…${url.slice(-15)}`;
}
