import React from 'react';
import { CloudRain, Play, RotateCcw } from 'lucide-react';
import type { MissionRow } from '../types';

interface WeatherHoldBannerProps {
  mission: MissionRow;
  onContinue: () => Promise<void>;
  onRecall: () => Promise<void>;
  isSending: boolean;
}

export const WeatherHoldBanner: React.FC<WeatherHoldBannerProps> = ({
  mission,
  onContinue,
  onRecall,
  isSending,
}) => {
  return (
    <div className="relative overflow-hidden rounded-xl border-2 border-tactical-warn bg-tactical-warn/10 p-4 sm:p-5 shadow-[0_0_25px_rgba(224,165,27,0.25)] animate-pulse-urgent mb-6">
      {/* Background glow pattern */}
      <div className="absolute -right-10 -top-10 w-36 h-36 rounded-full bg-tactical-warn/15 blur-2xl pointer-events-none" />

      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
        {/* Left: Weather warning text */}
        <div className="flex items-start gap-3">
          <div className="p-2.5 rounded-lg bg-tactical-warn/20 border border-tactical-warn text-tactical-warn shrink-0">
            <CloudRain className="w-6 h-6 animate-bounce" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <span className="px-2 py-0.5 text-[10px] font-black uppercase rounded bg-tactical-warn text-black">
                CRITICAL WEATHER HOLD
              </span>
              <span className="text-xs text-tactical-warn font-mono">
                Mission #{mission.id.slice(0, 8)}
              </span>
            </div>
            <h2 className="text-base sm:text-lg font-bold text-white mt-1">
              Awaiting Operator Confirmation
            </h2>
            <p className="text-xs sm:text-sm text-gray-300 mt-0.5 max-w-xl">
              Routine mission held pre-flight due to marginal weather conditions.
              Safety rules require explicit human decision to either proceed or recall the aircraft.
            </p>
          </div>
        </div>

        {/* Right: Continue / Recall Action Buttons */}
        <div className="flex items-center gap-2.5 w-full sm:w-auto shrink-0">
          <button
            onClick={onContinue}
            disabled={isSending}
            className="flex-1 sm:flex-none flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg bg-tactical-ok hover:bg-tactical-ok/90 text-black font-bold text-xs uppercase tracking-wider transition-all disabled:opacity-50 shadow-md active:scale-95"
          >
            <Play className="w-4 h-4 fill-current" />
            <span>Continue Mission</span>
          </button>

          <button
            onClick={onRecall}
            disabled={isSending}
            className="flex-1 sm:flex-none flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg bg-tactical-bad hover:bg-tactical-bad/90 text-white font-bold text-xs uppercase tracking-wider transition-all disabled:opacity-50 shadow-md active:scale-95"
          >
            <RotateCcw className="w-4 h-4" />
            <span>Recall to Dock</span>
          </button>
        </div>
      </div>
    </div>
  );
};
