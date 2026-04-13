import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { getNativeUrl } from '../api/client';

export interface MediaPlayerHandle {
  seekTo: (time: number) => void;
}

interface Props {
  docId: string;
  mediaType: 'video' | 'audio';
  onTimeUpdate?: (time: number) => void;
}

const MediaPlayer = forwardRef<MediaPlayerHandle, Props>(({ docId, mediaType, onTimeUpdate }, ref) => {
  const mediaElRef = useRef<HTMLVideoElement | HTMLAudioElement | null>(null);
  const [src, setSrc] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // The HTML5 media elements can't attach Authorization headers to their
  // `src` request, so we fetch an authenticated signed URL first and use
  // that as the source. The signed URL is valid for 60 minutes.
  useEffect(() => {
    let cancelled = false;
    setSrc(null);
    setError(null);
    getNativeUrl(docId)
      .then(({ url }) => {
        if (!cancelled) setSrc(url);
      })
      .catch(e => {
        if (!cancelled) setError(e?.message || 'Could not load media');
      });
    return () => { cancelled = true; };
  }, [docId]);

  useImperativeHandle(ref, () => ({
    seekTo: (time: number) => {
      if (mediaElRef.current) {
        mediaElRef.current.currentTime = time;
      }
    },
  }));

  const handleTimeUpdate = () => {
    if (onTimeUpdate && mediaElRef.current) {
      onTimeUpdate(mediaElRef.current.currentTime);
    }
  };

  if (error) {
    return (
      <div className="viewer-main">
        <div className="media-player-container" style={{ color: 'var(--color-danger-600)', fontSize: 'var(--text-sm)', padding: 'var(--space-4)', textAlign: 'center' }}>
          Could not load {mediaType}: {error}
        </div>
      </div>
    );
  }

  if (!src) {
    return (
      <div className="viewer-main">
        <div className="media-player-container">
          <div className="loading-center">
            <span className="spinner spinner-md" /> Loading {mediaType}…
          </div>
        </div>
      </div>
    );
  }

  if (mediaType === 'audio') {
    return (
      <div className="viewer-main">
        <div className="media-player-container media-player-audio">
          <audio
            ref={mediaElRef as React.RefObject<HTMLAudioElement>}
            controls
            src={src}
            style={{ width: '100%', maxWidth: 480 }}
            onTimeUpdate={handleTimeUpdate}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="viewer-main">
      <div className="media-player-container">
        <video
          ref={mediaElRef as React.RefObject<HTMLVideoElement>}
          controls
          src={src}
          style={{ width: '100%', maxHeight: '100%' }}
          onTimeUpdate={handleTimeUpdate}
        />
      </div>
    </div>
  );
});

MediaPlayer.displayName = 'MediaPlayer';
export default MediaPlayer;
