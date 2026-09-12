import React from 'react';
import { Radio, Battery, ShieldAlert, ShieldCheck, LogOut } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import { Badge } from './Badge';
import { getBatteryStatusClass } from '../lib/utils';
import type { DroneRow } from '../types';

interface HeaderProps {
  drone: DroneRow | null;
  isStale: boolean;
  staleSeconds: number;
  isLinkDown: boolean;
}

export const Header: React.FC<HeaderProps> = ({
  drone,
  isStale,
  staleSeconds,
  isLinkDown,
}) => {
  const { user, signOut } = useAuth();
  const batteryClass = getBatteryStatusClass(drone?.battery_pct);

  return (
    <header className="sticky top-0 z-40 bg-bg-card/95 backdrop-blur border-b border-bg-line px-4 py-3 sm:px-6">
      <div className="max-w-7xl mx-auto flex items-center justify-between gap-3">
        {/* Left: Brand & Drone Info */}
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-tactical-cyan/10 border border-tactical-cyan/30 flex items-center justify-center text-tactical-cyan">
            <Radio className="w-4 h-4 animate-pulse" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-sm sm:text-base font-bold tracking-wider text-white">
                GSS GROUND STATION
              </h1>
              <span className="text-[11px] text-gray-400 font-mono hidden sm:inline-block">
                [{drone?.name || 'Hexacopter-1'}]
              </span>
            </div>
            <div className="flex items-center gap-2 mt-0.5 text-xs">
              {/* Link indicator */}
              {isLinkDown ? (
                <span className="flex items-center gap-1 text-tactical-warn font-semibold text-[11px]">
                  <span className="w-2 h-2 rounded-full bg-tactical-warn animate-ping" />
                  LINK DOWN
                </span>
              ) : (
                <span className="flex items-center gap-1 text-tactical-ok font-semibold text-[11px]">
                  <span className="w-2 h-2 rounded-full bg-tactical-ok" />
                  LINK UP
                </span>
              )}

              <span className="text-gray-600">•</span>

              {/* Staleness indicator */}
              {isStale ? (
                <span className="text-tactical-bad font-bold text-[11px] animate-pulse">
                  STALE ({staleSeconds}s)
                </span>
              ) : (
                <span className="text-gray-400 text-[11px]">
                  TELEMETRY LIVE
                </span>
              )}
            </div>
          </div>
        </div>

        {/* Right: Quick Telemetry Pills & Sign Out */}
        <div className="flex items-center gap-2 sm:gap-3">
          {/* Armed Pill */}
          {drone?.armed !== undefined && (
            <Badge
              variant={drone.armed ? 'bad' : 'ok'}
              size="sm"
              className="hidden xs:inline-flex"
            >
              {drone.armed ? (
                <>
                  <ShieldAlert className="w-3 h-3 text-tactical-bad" />
                  ARMED
                </>
              ) : (
                <>
                  <ShieldCheck className="w-3 h-3 text-tactical-ok" />
                  DISARMED
                </>
              )}
            </Badge>
          )}

          {/* Battery Quick Pill */}
          {drone?.battery_pct != null && (
            <div
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-mono font-bold border ${batteryClass.bg}/20 ${batteryClass.text} border-current/30`}
            >
              <Battery className="w-3.5 h-3.5" />
              <span>{Math.round(drone.battery_pct)}%</span>
            </div>
          )}

          {/* User / Sign Out */}
          <button
            onClick={signOut}
            title={`Signed in as ${user?.email || 'Operator'}. Click to sign out.`}
            className="flex items-center gap-1 text-xs text-gray-400 hover:text-white bg-bg-input border border-bg-line hover:border-gray-600 px-2.5 py-1.5 rounded-lg transition-colors"
          >
            <LogOut className="w-3.5 h-3.5" />
            <span className="hidden md:inline">Sign Out</span>
          </button>
        </div>
      </div>
    </header>
  );
};
