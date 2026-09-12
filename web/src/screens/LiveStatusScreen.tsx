import React from 'react';
import {
  Battery,
  Zap,
  Navigation,
  Compass,
  Gauge,
  Satellite,
  MapPin,
  Flame,
  Volume2,
  Lightbulb,
  Camera,
  Layers,
  Home,
  AlertTriangle,
  WifiOff,
} from 'lucide-react';
import { Badge } from '../components/Badge';
import {
  formatNumber,
  formatDateTime,
  timeAgo,
  getBatteryStatusClass,
} from '../lib/utils';
import type { DroneRow, DockRow } from '../types';

interface LiveStatusScreenProps {
  drone: DroneRow | null;
  dock: DockRow | null;
  isStale: boolean;
  staleSeconds: number;
  isLinkDown: boolean;
  onNavigateToMap: () => void;
}

export const LiveStatusScreen: React.FC<LiveStatusScreenProps> = ({
  drone,
  dock,
  isStale,
  staleSeconds,
  isLinkDown,
  onNavigateToMap,
}) => {
  const batteryPct = drone?.battery_pct ?? null;
  const batteryClass = getBatteryStatusClass(batteryPct);

  return (
    <div className="space-y-5 max-w-7xl mx-auto pb-16">
      {/* 1. URGENT STALENESS BANNER (Prominent High Visibility) */}
      {isStale && (
        <div className="p-4 rounded-xl border-2 border-tactical-bad bg-tactical-bad/20 text-tactical-bad flex items-start gap-3.5 shadow-[0_0_20px_rgba(224,83,61,0.3)] animate-pulse-urgent">
          <AlertTriangle className="w-6 h-6 shrink-0 mt-0.5 animate-bounce text-tactical-bad" />
          <div>
            <div className="flex items-center gap-2">
              <span className="font-black text-sm uppercase tracking-wider bg-tactical-bad text-white px-2 py-0.5 rounded">
                CRITICAL STALE DATA WARNING
              </span>
              <span className="font-mono text-xs font-bold text-white">
                Last Telemetry {staleSeconds}s ago
              </span>
            </div>
            <p className="text-xs sm:text-sm text-gray-200 mt-1 font-medium">
              The Ground Station Service (GSS) is not hearing live telemetry updates from the vehicle.
              Numbers displayed below are NOT current and may represent past aircraft state.
            </p>
          </div>
        </div>
      )}

      {/* 2. RADIO LINK DOWN BANNER */}
      {isLinkDown && (
        <div className="p-4 rounded-xl border-2 border-tactical-warn bg-tactical-warn/20 text-tactical-warn flex items-start gap-3.5 shadow-[0_0_20px_rgba(224,165,27,0.25)]">
          <WifiOff className="w-6 h-6 shrink-0 mt-0.5 text-tactical-warn animate-pulse" />
          <div>
            <span className="font-black text-xs uppercase tracking-wider bg-tactical-warn text-black px-2 py-0.5 rounded">
              RADIO LINK DOWN (915 MHz)
            </span>
            <p className="text-xs sm:text-sm text-gray-200 mt-1 font-medium">
              The GSS dock radio is not receiving MAVLink heartbeats from the drone.
              Failsafe procedures engage automatically on vehicle onboard flight controller.
            </p>
          </div>
        </div>
      )}

      {/* 3. HERO OVERVIEW BAR */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {/* Status */}
        <div className="bg-bg-card border border-bg-line rounded-xl p-3.5">
          <div className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
            Vehicle Status
          </div>
          <div className="mt-1.5 flex items-center gap-2">
            <Badge
              variant={
                drone?.status === 'flying'
                  ? 'cyan'
                  : drone?.status === 'error'
                  ? 'bad'
                  : drone?.status === 'charging'
                  ? 'warn'
                  : 'ok'
              }
              size="md"
            >
              {drone?.status || 'UNKNOWN'}
            </Badge>
          </div>
        </div>

        {/* Flight Mode */}
        <div className="bg-bg-card border border-bg-line rounded-xl p-3.5">
          <div className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
            Flight Mode
          </div>
          <div className="mt-1.5 flex items-center gap-2">
            <Badge variant="blue" size="md">
              {drone?.mode || '—'}
            </Badge>
            <span className="text-xs text-gray-400 font-mono">
              ({drone?.control_mode || 'AUTO'})
            </span>
          </div>
        </div>

        {/* Arm State */}
        <div className="bg-bg-card border border-bg-line rounded-xl p-3.5">
          <div className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
            Motors Arm State
          </div>
          <div className="mt-1.5 flex items-center gap-2">
            <Badge variant={drone?.armed ? 'bad' : 'ok'} size="md">
              {drone?.armed ? 'ARMED' : 'DISARMED'}
            </Badge>
          </div>
        </div>

        {/* GPS Fix */}
        <div className="bg-bg-card border border-bg-line rounded-xl p-3.5">
          <div className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
            GPS Satellites / Fix
          </div>
          <div className="mt-1.5 flex items-center gap-2 text-white font-mono font-bold text-sm">
            <Satellite className="w-4 h-4 text-tactical-cyan" />
            <span>
              {drone?.gps_satellites != null ? `${drone.gps_satellites} sats` : '0 sat'}
            </span>
            <span className="text-xs text-gray-400">
              (Fix {drone?.gps_fix_type ?? 0})
            </span>
          </div>
        </div>
      </div>

      {/* 4. MAIN TELEMETRY GRID */}
      <div className={`grid grid-cols-1 lg:grid-cols-3 gap-5 ${isStale ? 'opacity-70' : ''}`}>
        {/* Left Col: Battery & Power HUD */}
        <div className="bg-bg-card border border-bg-line rounded-2xl p-5 shadow-lg space-y-4">
          <div className="flex items-center justify-between pb-3 border-b border-bg-line">
            <div className="flex items-center gap-2 text-white font-bold text-sm">
              <Battery className={`w-5 h-5 ${batteryClass.text}`} />
              <span>POWER & BATTERY</span>
            </div>
            {batteryPct != null && (
              <span className={`text-xs font-bold font-mono px-2 py-0.5 rounded ${batteryClass.bg}/20 ${batteryClass.text}`}>
                {batteryPct <= 20 ? 'CRITICAL FLOOR' : batteryPct <= 40 ? 'WARN THRESHOLD' : 'NORMAL'}
              </span>
            )}
          </div>

          {/* Big Battery Percentage */}
          <div className="flex items-baseline justify-between">
            <div className="text-4xl sm:text-5xl font-black font-mono tracking-tight text-white">
              {formatNumber(batteryPct, 0, '%')}
            </div>
            <div className="text-right">
              <div className="text-xs text-gray-400">Voltage / Current</div>
              <div className="text-sm font-mono font-bold text-gray-200 mt-0.5">
                {formatNumber(drone?.battery_voltage_v, 2, ' V')} •{' '}
                {formatNumber(drone?.battery_current_a, 1, ' A')}
              </div>
            </div>
          </div>

          {/* Colored Bar */}
          <div className="w-full bg-bg-input rounded-full h-3.5 p-0.5 border border-bg-line overflow-hidden">
            <div
              className={`h-full rounded-full transition-all duration-500 ${batteryClass.bg}`}
              style={{ width: `${Math.min(100, Math.max(0, batteryPct ?? 0))}%` }}
            />
          </div>

          {/* Threshold markers guide */}
          <div className="flex justify-between text-[10px] font-mono text-gray-500 px-1">
            <span>0%</span>
            <span className="text-tactical-bad">20% (Floor)</span>
            <span className="text-tactical-warn">40% (Warn)</span>
            <span>100%</span>
          </div>

          {/* Dock Status Card embedded */}
          <div className="mt-4 pt-4 border-t border-bg-line space-y-2">
            <div className="flex items-center justify-between text-xs">
              <span className="text-gray-400 flex items-center gap-1.5">
                <Home className="w-3.5 h-3.5 text-tactical-cyan" />
                Dock Status
              </span>
              <Badge variant={dock?.status === 'ok' ? 'ok' : 'warn'} size="sm">
                {dock?.status?.toUpperCase() || 'UNKNOWN'}
              </Badge>
            </div>
            <div className="flex items-center justify-between text-xs font-mono text-gray-300">
              <span>Drone In Dock</span>
              <span className={dock?.has_drone ? 'text-tactical-ok' : 'text-gray-500'}>
                {dock?.has_drone ? 'YES' : 'NO'}
              </span>
            </div>
            <div className="flex items-center justify-between text-xs font-mono text-gray-300">
              <span>Pogo Charging Active</span>
              <span className={dock?.charging_active ? 'text-tactical-warn animate-pulse font-bold' : 'text-gray-500'}>
                {dock?.charging_active ? 'CHARGING' : 'OFF'}
              </span>
            </div>
          </div>
        </div>

        {/* Center Col: Flight Dynamics (Altitude, Speed, Heading) */}
        <div className="bg-bg-card border border-bg-line rounded-2xl p-5 shadow-lg space-y-4">
          <div className="flex items-center justify-between pb-3 border-b border-bg-line">
            <div className="flex items-center gap-2 text-white font-bold text-sm">
              <Navigation className="w-5 h-5 text-tactical-cyan" />
              <span>FLIGHT DYNAMICS</span>
            </div>
            <span className="text-xs text-gray-400 font-mono">
              Live Telemetry
            </span>
          </div>

          <div className="grid grid-cols-2 gap-3">
            {/* Relative Altitude */}
            <div className="bg-bg-input border border-bg-line rounded-xl p-3">
              <div className="text-[11px] text-gray-400 uppercase tracking-wider flex items-center gap-1">
                <Gauge className="w-3.5 h-3.5 text-tactical-cyan" />
                Altitude (Rel)
              </div>
              <div className="text-2xl font-mono font-bold text-white mt-1">
                {formatNumber(drone?.alt_m_relative, 1, ' m')}
              </div>
              <div className="text-[10px] text-gray-500 font-mono mt-0.5">
                AMSL: {formatNumber(drone?.alt_m_amsl, 1, ' m')}
              </div>
            </div>

            {/* Ground Speed */}
            <div className="bg-bg-input border border-bg-line rounded-xl p-3">
              <div className="text-[11px] text-gray-400 uppercase tracking-wider flex items-center gap-1">
                <Zap className="w-3.5 h-3.5 text-tactical-warn" />
                Ground Speed
              </div>
              <div className="text-2xl font-mono font-bold text-white mt-1">
                {formatNumber(drone?.groundspeed_ms, 1, ' m/s')}
              </div>
              <div className="text-[10px] text-gray-500 font-mono mt-0.5">
                {formatNumber((drone?.groundspeed_ms ?? 0) * 3.6, 1, ' km/h')}
              </div>
            </div>

            {/* Heading & Compass */}
            <div className="bg-bg-input border border-bg-line rounded-xl p-3">
              <div className="text-[11px] text-gray-400 uppercase tracking-wider flex items-center gap-1">
                <Compass className="w-3.5 h-3.5 text-tactical-ok" />
                Heading
              </div>
              <div className="text-2xl font-mono font-bold text-white mt-1">
                {drone?.heading_deg != null ? `${Math.round(drone.heading_deg)}°` : '—'}
              </div>
              <div className="text-[10px] text-gray-500 font-mono mt-0.5">
                Magnetic Yaw
              </div>
            </div>

            {/* Position Fix Indicator */}
            <div className="bg-bg-input border border-bg-line rounded-xl p-3">
              <div className="text-[11px] text-gray-400 uppercase tracking-wider flex items-center gap-1">
                <Satellite className="w-3.5 h-3.5 text-tactical-blue" />
                Position Fix
              </div>
              <div className="text-lg font-mono font-bold text-white mt-1.5 truncate">
                {drone?.position_valid ? (
                  <span className="text-tactical-ok">VALID FIX</span>
                ) : (
                  <span className="text-tactical-bad">NO 3D FIX</span>
                )}
              </div>
              <div className="text-[10px] text-gray-500 font-mono mt-0.5">
                RTK / GPS 3D
              </div>
            </div>
          </div>

          {/* Current GPS Coordinates Bar */}
          <div className="bg-bg-input border border-bg-line rounded-xl p-3 flex items-center justify-between">
            <div>
              <div className="text-[10px] text-gray-400 uppercase font-semibold">
                Current Coordinates
              </div>
              <div className="text-xs font-mono font-bold text-tactical-cyan mt-0.5">
                {drone?.position_valid && drone?.lat != null && drone?.lon != null
                  ? `${Number(drone.lat).toFixed(6)}, ${Number(drone.lon).toFixed(6)}`
                  : 'Coordinates not fixed'}
              </div>
            </div>
            <button
              onClick={onNavigateToMap}
              className="px-2.5 py-1.5 rounded-lg bg-tactical-cyan/15 hover:bg-tactical-cyan/25 text-tactical-cyan border border-tactical-cyan/30 text-xs font-semibold flex items-center gap-1 transition-colors"
            >
              <MapPin className="w-3.5 h-3.5" />
              <span>Map</span>
            </button>
          </div>
        </div>

        {/* Right Col: Last Trustworthy Fix & Read-Only Payload Sensors */}
        <div className="bg-bg-card border border-bg-line rounded-2xl p-5 shadow-lg space-y-4">
          {/* Last Trustworthy Fix */}
          <div className="pb-3 border-b border-bg-line">
            <div className="flex items-center gap-2 text-white font-bold text-sm">
              <MapPin className="w-5 h-5 text-tactical-warn" />
              <span>LAST KNOWN POSITION</span>
            </div>
            <div className="mt-2 text-sm font-mono font-bold text-gray-200">
              {drone?.last_known_lat != null && drone?.last_known_lon != null ? (
                <>
                  <div>
                    {Number(drone.last_known_lat).toFixed(6)},{' '}
                    {Number(drone.last_known_lon).toFixed(6)}
                  </div>
                  <div className="text-xs text-gray-400 font-normal mt-0.5">
                    Alt: {formatNumber(drone.last_known_alt_m, 1, ' m')} • Fixed{' '}
                    {timeAgo(drone.last_known_at)} ({formatDateTime(drone.last_known_at)})
                  </div>
                </>
              ) : (
                <div className="text-gray-500">Never had a valid GPS fix</div>
              )}
            </div>
            <p className="text-[11px] text-gray-500 mt-1">
              * Kept from the last valid GPS fix. Not cleared when radio link drops.
            </p>
          </div>

          {/* Payload & Sensor Status (Read-Only) */}
          <div>
            <div className="flex items-center justify-between mb-2.5">
              <div className="flex items-center gap-2 text-white font-bold text-xs">
                <Layers className="w-4 h-4 text-gray-400" />
                <span>PAYLOAD & SENSORS (READ-ONLY)</span>
              </div>
              <span className="text-[10px] text-gray-500 uppercase">
                Hardware Pending
              </span>
            </div>

            <div className="grid grid-cols-2 gap-2 text-xs">
              <div className="bg-bg-input border border-bg-line rounded-lg p-2 flex items-center justify-between">
                <span className="text-gray-400 flex items-center gap-1.5">
                  <Lightbulb className="w-3.5 h-3.5" /> Spotlight
                </span>
                <span className={drone?.spotlight_on ? 'text-tactical-warn font-bold' : 'text-gray-600'}>
                  {drone?.spotlight_on ? 'ON' : 'OFF'}
                </span>
              </div>

              <div className="bg-bg-input border border-bg-line rounded-lg p-2 flex items-center justify-between">
                <span className="text-gray-400 flex items-center gap-1.5">
                  <Volume2 className="w-3.5 h-3.5" /> Siren 100dB
                </span>
                <span className={drone?.siren_on ? 'text-tactical-bad font-bold' : 'text-gray-600'}>
                  {drone?.siren_on ? 'ON' : 'OFF'}
                </span>
              </div>

              <div className="bg-bg-input border border-bg-line rounded-lg p-2 flex items-center justify-between">
                <span className="text-gray-400 flex items-center gap-1.5">
                  <Flame className="w-3.5 h-3.5" /> Beacon
                </span>
                <span className={drone?.beacon_active ? 'text-tactical-warn font-bold' : 'text-gray-600'}>
                  {drone?.beacon_active ? 'ACTIVE' : 'OFF'}
                </span>
              </div>

              <div className="bg-bg-input border border-bg-line rounded-lg p-2 flex items-center justify-between">
                <span className="text-gray-400 flex items-center gap-1.5">
                  <Camera className="w-3.5 h-3.5" /> Recording
                </span>
                <span className={drone?.recording_active ? 'text-tactical-ok font-bold' : 'text-gray-600'}>
                  {drone?.recording_active ? 'REC' : 'OFF'}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Telemetry timestamp footer */}
      <div className="text-center text-xs font-mono text-gray-500 pt-2">
        Last Telemetry Update:{' '}
        <span className="text-gray-300">
          {drone?.last_telemetry_at
            ? `${formatDateTime(drone.last_telemetry_at)} (${timeAgo(drone.last_telemetry_at)})`
            : 'No telemetry yet'}
        </span>
      </div>
    </div>
  );
};
