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
 * 在 Desktop 文本中保留完整 URL 目标，同时用紧凑文案展示并经受控 IPC 打开。
 *
 * Args:
 *   props: 需要识别 HTTP(S) 链接的原始文本。
 *
 * Returns:
 *   普通文本与可点击链接交错组成的行内内容。
 */
export function LinkifiedText({ text }: LinkifiedTextProps): JSX.Element {
  return (
    <>
      {splitLinkifiedText(text).map((part, index) => {
        if (part.kind === "text") {
          return <Fragment key={`text-${index}`}>{part.value}</Fragment>;
        }
        return (
          <button
            className="linkified-text-url"
            key={`url-${index}`}
            role="link"
            type="button"
            title={part.value}
            onClick={() => {
              void window.companionChat?.openExternalUrl?.(part.value).catch(error => {
                window.companionChat?.reportDiagnostic(
                  "warn",
                  "chat.external_url_open_failed",
                  error
                );
              });
            }}
          >
            {shortenUrl(part.value)}
          </button>
        );
      })}
    </>
  );
}

/**
 * 把正文拆成不会丢失原字符的普通文本和 HTTP(S) URL 片段。
 *
 * Args:
 *   text: 原始展示正文。
 *
 * Returns:
 *   按原顺序排列的文本片段。
 */
function splitLinkifiedText(text: string): TextPart[] {
  const parts: TextPart[] = [];
  let cursor = 0;
  for (const match of text.matchAll(HTTP_URL_PATTERN)) {
    const matchIndex = match.index ?? cursor;
    const rawMatch = match[0];
    const url = trimUrlPunctuation(rawMatch);
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
      const trimmed = trimUnbalancedClosingBracket(candidate, opening, closing);
      if (trimmed !== candidate) {
        candidate = trimmed;
        changed = true;
        break;
      }
    }
  }
  return candidate;
}

function trimUnbalancedClosingBracket(value: string, opening: string, closing: string): string {
  let candidate = value;
  while (
    candidate.endsWith(closing) &&
    countCharacter(candidate, closing) > countCharacter(candidate, opening)
  ) {
    candidate = candidate.slice(0, -1);
  }
  return candidate;
}

function countCharacter(value: string, target: string): number {
  return [...value].filter(character => character === target).length;
}

function shortenUrl(url: string): string {
  const maxLength = 52;
  if (url.length <= maxLength) {
    return url;
  }
  return `${url.slice(0, 34)}…${url.slice(-15)}`;
}
