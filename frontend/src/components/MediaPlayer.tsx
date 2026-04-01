import { forwardRef, useImperativeHandle, useRef } from 'react';
import { streamUrl } from '../api/client';

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
  const src = streamUrl(docId);

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
