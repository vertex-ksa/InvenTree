import { t } from '@lingui/core/macro';
import { Button } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api } from '../../App';
import { useUserState } from '../../states/UserState';

/** The native count grant and enabled server policy control discoverability. */
export default function CycleCountNavigation() {
  const actorId = useUserState((state) => state.user?.pk);
  const capability = useQuery({
    queryKey: ['cycle-count-locations', actorId],
    queryFn: async () => (await api.get('/api/stock/cycle-count/')).data,
    enabled: !!actorId,
    retry: false,
    staleTime: 60000
  });

  if (!capability.data) return null;
  return (
    <Button component={Link} to='/stock/cycle-count/' variant='subtle' h={44}>
      {t`Cycle count`}
    </Button>
  );
}
