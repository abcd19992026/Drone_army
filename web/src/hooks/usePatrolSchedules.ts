import { useState, useEffect, useCallback } from 'react';
import { supabase } from '../lib/supabase';
import type { PatrolScheduleRow, PatrolScheduleFormData } from '../types';

export function usePatrolSchedules() {
  const [schedules, setSchedules] = useState<PatrolScheduleRow[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [actionLoading, setActionLoading] = useState<boolean>(false);
  const [statusMessage, setStatusMessage] = useState<{
    text: string;
    type: 'info' | 'success' | 'error';
  } | null>(null);

  const fetchSchedules = useCallback(async () => {
    try {
      const { data, error } = await supabase
        .from('patrol_schedules')
        .select('*')
        .order('created_at', { ascending: false });

      if (!error && data) {
        setSchedules(data as PatrolScheduleRow[]);
      } else if (error) {
        console.error('Error loading patrol schedules:', error.message);
      }
    } catch (err) {
      console.error('Error fetching patrol schedules:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSchedules();

    const channel = supabase
      .channel('gss-patrol-schedules')
      .on(
        'postgres_changes',
        {
          event: '*',
          schema: 'public',
          table: 'patrol_schedules',
        },
        () => {
          fetchSchedules();
        }
      )
      .subscribe();

    return () => {
      supabase.removeChannel(channel);
    };
  }, [fetchSchedules]);

  const createSchedule = async (
    formData: PatrolScheduleFormData
  ): Promise<{ success: boolean; error?: string }> => {
    setActionLoading(true);
    setStatusMessage({ text: 'Creating patrol schedule...', type: 'info' });

    try {
      const payload: Record<string, any> = {
        name: formData.name.trim(),
        target_lat: Number(formData.target_lat),
        target_lon: Number(formData.target_lon),
        cruise_alt_m:
          formData.cruise_alt_m !== '' && formData.cruise_alt_m !== null
            ? Number(formData.cruise_alt_m)
            : null,
        loiter_seconds:
          formData.loiter_seconds !== ''
            ? Number(formData.loiter_seconds)
            : 120,
        cadence: formData.cadence,
        time_of_day: formData.time_of_day,
        days_of_week:
          formData.cadence === 'weekly'
            ? [...formData.days_of_week].sort((a, b) => a - b)
            : null,
        active: formData.active,
        // Provide epoch 0 to satisfy Postgres NOT NULL constraint; backend scheduler maintains next_run_at
        next_run_at: '1970-01-01T00:00:00Z',
      };

      const { error } = await supabase.from('patrol_schedules').insert(payload);

      if (error) {
        const errorMsg = `Failed to create schedule: ${error.message}`;
        setStatusMessage({ text: errorMsg, type: 'error' });
        setActionLoading(false);
        return { success: false, error: errorMsg };
      }

      setStatusMessage({
        text: `✓ Schedule "${formData.name}" created successfully!`,
        type: 'success',
      });
      setActionLoading(false);
      fetchSchedules();
      return { success: true };
    } catch (err: any) {
      const msg = err.message || 'Unknown network error';
      setStatusMessage({ text: `Create error: ${msg}`, type: 'error' });
      setActionLoading(false);
      return { success: false, error: msg };
    }
  };

  const updateSchedule = async (
    id: string,
    formData: PatrolScheduleFormData
  ): Promise<{ success: boolean; error?: string }> => {
    setActionLoading(true);
    setStatusMessage({ text: 'Updating patrol schedule...', type: 'info' });

    try {
      const payload: Record<string, any> = {
        name: formData.name.trim(),
        target_lat: Number(formData.target_lat),
        target_lon: Number(formData.target_lon),
        cruise_alt_m:
          formData.cruise_alt_m !== '' && formData.cruise_alt_m !== null
            ? Number(formData.cruise_alt_m)
            : null,
        loiter_seconds:
          formData.loiter_seconds !== ''
            ? Number(formData.loiter_seconds)
            : 120,
        cadence: formData.cadence,
        time_of_day: formData.time_of_day,
        days_of_week:
          formData.cadence === 'weekly'
            ? [...formData.days_of_week].sort((a, b) => a - b)
            : null,
        active: formData.active,
        // Reset next_run_at to epoch so backend re-calculates the next valid occurrence for updated time
        next_run_at: '1970-01-01T00:00:00Z',
      };

      const { error } = await supabase
        .from('patrol_schedules')
        .update(payload)
        .eq('id', id);

      if (error) {
        const errorMsg = `Failed to update schedule: ${error.message}`;
        setStatusMessage({ text: errorMsg, type: 'error' });
        setActionLoading(false);
        return { success: false, error: errorMsg };
      }

      setStatusMessage({
        text: `✓ Schedule "${formData.name}" updated successfully!`,
        type: 'success',
      });
      setActionLoading(false);
      fetchSchedules();
      return { success: true };
    } catch (err: any) {
      const msg = err.message || 'Unknown network error';
      setStatusMessage({ text: `Update error: ${msg}`, type: 'error' });
      setActionLoading(false);
      return { success: false, error: msg };
    }
  };

  const toggleScheduleActive = async (
    id: string,
    currentActive: boolean
  ): Promise<{ success: boolean; error?: string }> => {
    try {
      const nextActive = !currentActive;
      const { error } = await supabase
        .from('patrol_schedules')
        .update({ active: nextActive })
        .eq('id', id);

      if (error) {
        const errorMsg = `Failed to toggle status: ${error.message}`;
        setStatusMessage({ text: errorMsg, type: 'error' });
        return { success: false, error: errorMsg };
      }

      setStatusMessage({
        text: `✓ Schedule ${nextActive ? 'activated' : 'paused'}.`,
        type: 'success',
      });
      fetchSchedules();
      return { success: true };
    } catch (err: any) {
      const msg = err.message || 'Failed to toggle status';
      setStatusMessage({ text: msg, type: 'error' });
      return { success: false, error: msg };
    }
  };

  const deleteSchedule = async (
    id: string
  ): Promise<{ success: boolean; error?: string }> => {
    setActionLoading(true);
    setStatusMessage({ text: 'Deleting schedule...', type: 'info' });

    try {
      const { error } = await supabase
        .from('patrol_schedules')
        .delete()
        .eq('id', id);

      if (error) {
        const errorMsg = `Failed to delete schedule: ${error.message}`;
        setStatusMessage({ text: errorMsg, type: 'error' });
        setActionLoading(false);
        return { success: false, error: errorMsg };
      }

      setStatusMessage({
        text: '✓ Schedule deleted successfully.',
        type: 'success',
      });
      setActionLoading(false);
      fetchSchedules();
      return { success: true };
    } catch (err: any) {
      const msg = err.message || 'Unknown error while deleting';
      setStatusMessage({ text: `Delete error: ${msg}`, type: 'error' });
      setActionLoading(false);
      return { success: false, error: msg };
    }
  };

  return {
    schedules,
    loading,
    actionLoading,
    statusMessage,
    clearStatusMessage: () => setStatusMessage(null),
    createSchedule,
    updateSchedule,
    toggleScheduleActive,
    deleteSchedule,
    refetch: fetchSchedules,
  };
}
