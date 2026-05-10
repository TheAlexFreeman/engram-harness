import { useRef, useState, useEffect } from "react";
import { MarkdownContent } from "./MarkdownContent";

interface Props {
  text: string;
  streaming?: boolean;
}

export function StreamingText({ text, streaming = false }: Props) {
  const [displayed, setDisplayed] = useState(text);
  const rafRef = useRef<number | undefined>(undefined);
  const pendingRef = useRef(text);

  useEffect(() => {
    pendingRef.current = text;
    if (!rafRef.current) {
      rafRef.current = requestAnimationFrame(() => {
        setDisplayed(pendingRef.current);
        rafRef.current = undefined;
      });
    }
    return () => {
      if (rafRef.current) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = undefined;
      }
    };
  }, [text]);

  return (
    <div className="relative">
      <MarkdownContent content={displayed} />
      {streaming && (
        <span className="cursor-blink ml-0.5 inline-block w-1.5 h-4 bg-green-400 align-middle" />
      )}
    </div>
  );
}
