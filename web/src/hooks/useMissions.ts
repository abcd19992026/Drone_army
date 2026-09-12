import { useState, useEffect, useCallback } from 'react';
import { supabase } from '../lib/supabase';
import { APP_CONFIG } from '../lib/config';
import type { MissionRow, MissionEventRow } from '../types';

export function useMissions() {
  const [missions, setMissions] = useState<MissionRow[]>([]);
  const [activeMission, setActiveMission] = useState<MissionRow | null>(null);
  const [weatherHoldMission, setWeatherHoldMission] = useState<MissionRow | null>(null);
  const [selectedMissionEvents, setSelectedMissionEvents] = useState<MissionEventRow[]>([]);
  const [loadingEvents, setLoadingEvents] = useState<boolean>(false);
  const [loading, setLoading] = useState<boolean>(true);

  const fetchMissions = useCallback(async () => {
    try {
      const { data, error } = await supabase
        .from('missions')
        .select('*')
        .eq('drone_id', APP_CONFIG.DRONE_ID)
        .order('created_at', { ascending: false })
        .limit(25);

      if (!error && data) {
        const missionList = data as MissionRow[];
        setMissions(missionList);

        // Active mission (any state other than landed or aborted)
        const ongoing = missionList.find(
          (m) => m.status !== 'landed' && m.status !== 'aborted'
        );
        setActiveMission(ongoing || null);

        // Specifically check for weather hold
        const hold = missionList.find((m) => m.status === 'awaiting_confirmation');
        setWeatherHoldMission(hold || null);
      }
    } catch (err) {
      console.error('Error fetching missions:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchMissionEvents = async (missionId: string) => {
    setLoadingEvents(true);
    try {
      const { data, error } = await supabase
        .from('mission_events')
        .select('*')
        .eq('mission_id', missionId)
        .order('at', { ascending: true });

      if (!error && data) {
        setSelectedMissionEvents(data as MissionEventRow[]);
      } else {
        setSelectedMissionEvents([]);
      }
    } catch (err) {
      console.error('Error fetching mission events:', err);
      setSelectedMissionEvents([]);
    } finally {
      setLoadingEvents(false);
    }
  };

  useEffect(() => {
    fetchMissions();

    const channel = supabase
      .channel(`gss-missions-${APP_CONFIG.DRONE_ID}`)
      .on(
        'postgres_changes',
        {
          event: '*',
          schema: 'public',
          table: 'missions',
          filter: `drone_id=eq.${APP_CONFIG.DRONE_ID}`,
        },
        () => {
          fetchMissions();
        }
      )
      .subscribe();

    return () => {
      supabase.removeChannel(channel);
    };
  }, [fetchMissions]);

  return {
    missions,
    activeMission,
    weatherHoldMission,
    selectedMissionEvents,
    loadingEvents,
    loading,
    fetchMissionEvents,
    refetch: fetchMissions,
  };
}
