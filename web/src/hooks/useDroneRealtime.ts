import { useEffect, useState, useCallback, useRef } from 'react';
import { supabase } from '../lib/supabase';
import { APP_CONFIG } from '../lib/config';
import type { DroneRow, DockRow } from '../types';

export function useDroneRealtime() {
  const [drone, setDrone] = useState<DroneRow | null>(null);
  const [dock, setDock] = useState<DockRow | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const [isStale, setIsStale] = useState<boolean>(false);
  const [staleSeconds, setStaleSeconds] = useState<number>(0);

  const droneRef = useRef<DroneRow | null>(null);

  const checkStaleness = useCallback((currentDrone: DroneRow | null) => {
    if (!currentDrone || !currentDrone.last_telemetry_at) {
      setIsStale(true);
      setStaleSeconds(999);
      return;
    }
    const ageMs = Date.now() - new Date(currentDrone.last_telemetry_at).getTime();
    const ageSec = Math.max(0, Math.round(ageMs / 1000));
    setStaleSeconds(ageSec);

    // Dynamic threshold:
    // When airborne/armed: strict 5s threshold for flight safety
    // When docked/disarmed: 20s threshold for stationary heartbeat cycle
    const isAirborne =
      currentDrone.armed === true ||
      currentDrone.status === 'flying' ||
      currentDrone.status === 'returning';

    const thresholdMs = isAirborne
      ? APP_CONFIG.STALE_THRESHOLD_FLYING_MS
      : APP_CONFIG.STALE_THRESHOLD_DOCKED_MS;

    setIsStale(ageMs > thresholdMs);
  }, []);

  const updateDroneState = useCallback(
    (newDrone: DroneRow) => {
      droneRef.current = newDrone;
      setDrone(newDrone);
      checkStaleness(newDrone);
    },
    [checkStaleness]
  );

  const fetchDroneAndDock = useCallback(async () => {
    try {
      // Fetch Drone
      const droneRes = await supabase
        .from('drones')
        .select('*')
        .eq('id', APP_CONFIG.DRONE_ID)
        .maybeSingle();

      if (droneRes.error) {
        setError(droneRes.error.message);
      } else if (droneRes.data) {
        const droneData = droneRes.data as DroneRow;
        updateDroneState(droneData);
      }

      // Fetch Dock
      const dockRes = await supabase
        .from('docks')
        .select('*')
        .limit(1)
        .maybeSingle();

      if (!dockRes.error && dockRes.data) {
        setDock(dockRes.data as DockRow);
      }
    } catch (err: any) {
      setError(err.message || 'Failed to fetch drone status');
    } finally {
      setLoading(false);
    }
  }, [updateDroneState]);

  useEffect(() => {
    // Initial fetch
    fetchDroneAndDock();

    // 1. Supabase Realtime channel for instant live pushes
    const channel = supabase
      .channel(`gss-drone-channel-${APP_CONFIG.DRONE_ID}`)
      .on(
        'postgres_changes',
        {
          event: '*',
          schema: 'public',
          table: 'drones',
          filter: `id=eq.${APP_CONFIG.DRONE_ID}`,
        },
        (payload) => {
          if (payload.new) {
            updateDroneState(payload.new as DroneRow);
          }
        }
      )
      .on(
        'postgres_changes',
        {
          event: '*',
          schema: 'public',
          table: 'docks',
        },
        (payload) => {
          if (payload.new) {
            setDock(payload.new as DockRow);
          }
        }
      )
      .subscribe((status) => {
        if (status === 'SUBSCRIBED') {
          fetchDroneAndDock();
        }
      });

    // 2. 1-second watchdog for staleness timer tick and dynamic high-frequency flight polling
    let lastPollAt = 0;
    const watchdogInterval = setInterval(() => {
      const current = droneRef.current;
      checkStaleness(current);

      // Adaptive polling cadence:
      // When airborne/armed/returning: poll every 1000ms (1s) to ensure real-time map tracking
      // When docked/stationary: poll every 3000ms (3s)
      const isAirborne =
        current?.armed === true ||
        current?.status === 'flying' ||
        current?.status === 'returning' ||
        (current?.alt_m_relative != null && Number(current.alt_m_relative) > 1.5) ||
        (current?.groundspeed_ms != null && Number(current.groundspeed_ms) > 0.5);

      const requiredIntervalMs = isAirborne ? 1000 : 3000;
      const now = Date.now();
      if (now - lastPollAt >= requiredIntervalMs) {
        lastPollAt = now;
        fetchDroneAndDock();
      }
    }, 500);

    return () => {
      supabase.removeChannel(channel);
      clearInterval(watchdogInterval);
    };
  }, [fetchDroneAndDock, checkStaleness, updateDroneState]);

  const isLinkDown = drone ? drone.link_up === false : false;

  return {
    drone,
    dock,
    loading,
    error,
    isStale,
    staleSeconds,
    isLinkDown,
    refetch: fetchDroneAndDock,
  };
}
