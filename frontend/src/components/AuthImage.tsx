import { useEffect, useState } from 'react';
import { fetchImageBlob } from '../api/client';

interface Props {
  docId: string;
  pageNum: number;
  width?: number;
  alt?: string;
  style?: React.CSSProperties;
  className?: string;
  loading?: 'lazy' | 'eager';
}

export default function AuthImage({ docId, pageNum, width, alt, style, className, loading }: Props) {
  const [src, setSrc] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchImageBlob(docId, pageNum, width)
      .then(url => { if (!cancelled) setSrc(url); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [docId, pageNum, width]);

  if (!src) {
    return <div className={className} style={{ ...style, background: 'var(--color-neutral-100)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <span className="spinner spinner-sm" />
    </div>;
  }

  return <img src={src} alt={alt} style={style} className={className} loading={loading} />;
}
