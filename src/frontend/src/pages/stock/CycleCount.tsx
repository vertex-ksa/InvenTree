import { i18n } from '@lingui/core';
import { t } from '@lingui/core/macro';
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Group,
  Modal,
  Paper,
  Select,
  Skeleton,
  Stack,
  Text,
  TextInput,
  Title
} from '@mantine/core';
import { useWindowEvent } from '@mantine/hooks';
import { useQuery } from '@tanstack/react-query';
import { type FormEvent, useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api } from '../../App';
import { PageDetail } from '../../components/nav/PageDetail';
import { useUserState } from '../../states/UserState';

type Item = {
  id: number;
  partName: string;
  partIPN: string;
  partRevision: string;
  batch: string;
  unit: string;
  observed: string | null;
};
type Observer = {
  sessionId: number;
  state: string;
  revision: number;
  location: { id: number; name: string };
  review: {
    requestId: number;
    state: string;
    revision: number;
    expiresAt: string;
  } | null;
  items: Item[];
};
type ReviewItem = Omit<Item, 'id' | 'observed'> & {
  item: string;
  expected: string;
  counted: string;
  delta: string;
};
type Reviewer = {
  location: { id: number; name: string };
  sessionId: number;
  state: string;
  countRevision: number;
  requestId: number;
  approvalRevision: number;
  approvalState: string;
  materialStale: boolean;
  policyVersion: string;
  expiresAt: string;
  items: ReviewItem[];
};

const endpoint = '/api/stock/cycle-count/';

function stateLabel(state: string) {
  switch (state) {
    case 'OPEN':
      return t`Counting`;
    case 'PENDING':
      return t`Awaiting review`;
    case 'APPROVED':
      return t`Approved; not applied`;
    case 'COMMITTED':
    case 'CONSUMED':
      return t`Count applied`;
    case 'REJECTED':
      return t`Review rejected`;
    case 'REVOKED':
      return t`Approval revoked`;
    case 'EXPIRED':
      return t`Approval expired`;
    default:
      return t`Awaiting review`;
  }
}

function failureMessage(error: unknown) {
  const status = (error as { response?: { status?: number } }).response?.status;
  if (status === 404)
    return t`This count is unavailable or cycle counting is disabled. Ask your administrator.`;
  if (status === 403)
    return t`You no longer have access to this count or action. Your entered values are retained.`;
  if (status === 409)
    return t`The count, approval, or stock changed. Refresh to check the saved state. A new count may be required.`;
  if (status === 400)
    return t`The entry could not be saved. Enter a nonnegative quantity with at most five decimal places and check the reason.`;
  if (status === 503)
    return t`The count policy is unavailable. Ask your administrator; entered values are retained.`;
  return t`The result could not be confirmed. Refresh to check the saved state, or retry the same action. Your entered values are retained.`;
}

function ItemIdentity({ item }: { item: Item | ReviewItem }) {
  return (
    <Stack gap={4}>
      <Text fw={600}>{item.partName}</Text>
      <Text size='sm' c='dimmed'>
        {t`Stock item`}:{' '}
        <bdi translate='no'>{'id' in item ? item.id : item.item}</bdi> ·{' '}
        {t`SKU`}: <bdi translate='no'>{item.partIPN || '—'}</bdi> ·{' '}
        {t`Revision`}: <bdi translate='no'>{item.partRevision || '—'}</bdi> ·{' '}
        {t`Batch`}: <bdi translate='no'>{item.batch || '—'}</bdi>
      </Text>
    </Stack>
  );
}

/** A durable native task whose observer surface never requests stock balances. */
export default function CycleCount() {
  const { id, view } = useParams();
  const actorId = useUserState((state) => state.user?.pk);
  return <CountTask key={JSON.stringify([actorId, id, view])} />;
}
function CountTask() {
  const { id, view } = useParams();
  const reviewer = view === 'review';
  const navigate = useNavigate();
  const actorId = useUserState((state) => state.user?.pk);
  const [location, setLocation] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<number, string>>({});
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [refreshNeeded, setRefreshNeeded] = useState(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const blocked = busy || refreshNeeded;
  const [activeAction, setActiveAction] = useState<string | number>('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [confirm, setConfirm] = useState(false);
  // Keep a command for the exact payload after a lost acknowledgement. A changed
  // quantity/revision/reason becomes a different command, never an overwrite.
  const commands = useRef(new Map<string, string>());
  const inputs = useRef(new Map<number, HTMLInputElement>());
  const reviewButton = useRef<HTMLButtonElement>(null);
  const focusNext = useRef(false);

  const locations = useQuery({
    queryKey: ['cycle-count-locations', actorId],
    queryFn: async () =>
      (await api.get<{ locations: { id: number; name: string }[] }>(endpoint))
        .data,
    enabled: !id,
    retry: false
  });
  const task = useQuery({
    queryKey: ['cycle-count-task', actorId, id, reviewer],
    queryFn: async () =>
      (
        await api.get<Observer | Reviewer>(
          `${endpoint}${id}/${reviewer ? 'review/' : ''}`
        )
      ).data,
    enabled: !!id,
    retry: false
  });
  const observer = !reviewer ? (task.data as Observer | undefined) : undefined;
  const review = reviewer ? (task.data as Reviewer | undefined) : undefined;
  const currentState =
    review?.approvalState ?? observer?.review?.state ?? observer?.state ?? '';
  const terminal = ['REJECTED', 'REVOKED', 'EXPIRED'].includes(currentState);
  const applied = ['COMMITTED', 'CONSUMED'].includes(currentState);
  const pending =
    observer?.items.filter((item) => item.observed === null).length ?? 0;
  const unsaved =
    observer?.items.some(
      (item) => item.observed === null && !!drafts[item.id]?.trim()
    ) ||
    (!!reason.trim() && !terminal && !applied);
  useWindowEvent('beforeunload', (event) => {
    if (unsaved) event.preventDefault();
  });

  useEffect(() => {
    if (!focusNext.current || !observer) return;
    focusNext.current = false;
    const next = observer.items.find((item) => item.observed === null);
    if (next) inputs.current.get(next.id)?.focus();
    else reviewButton.current?.focus();
  }, [observer]);

  async function command(
    path: string,
    payload: Record<string, unknown>,
    success: string
  ) {
    if (blocked) return;

    setBusy(true);
    setActiveAction(
      (payload.decision ??
        payload.item_id ??
        (path.endsWith('/request/') ? 'REQUEST' : 'COMMIT')) as string | number
    );
    setError('');
    setNotice('');
    const identity = JSON.stringify({ actorId, path, payload });
    let commandId = commands.current.get(identity);
    if (!commandId) {
      commandId = crypto.randomUUID();
      commands.current.set(identity, commandId);
    }
    try {
      await api.post(path, { ...payload, command_id: commandId });
      if (!alive.current) return;
      setRefreshNeeded(true);
      if (payload.item_id) focusNext.current = true;
      if (payload.decision) setReason('');
      setNotice(success);
      setConfirm(false);
      await task.refetch({ throwOnError: true });
      if (!alive.current) return;
      setRefreshNeeded(false);
    } catch (exception) {
      if (alive.current) setError(failureMessage(exception));
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  async function start(event: FormEvent) {
    event.preventDefault();

    if (!location || blocked) return;

    setBusy(true);
    setError('');
    try {
      const response = await api.post<Observer>(endpoint, {
        location_id: Number(location)
      });

      if (alive.current)
        navigate(`/stock/cycle-count/${response.data.sessionId}/`);
    } catch (exception) {
      if (alive.current) setError(failureMessage(exception));
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  async function refresh() {
    setError('');

    try {
      await (id
        ? task.refetch({ throwOnError: true })
        : locations.refetch({ throwOnError: true }));

      if (alive.current) setRefreshNeeded(false);
    } catch (exception) {
      if (alive.current) setError(failureMessage(exception));
    }
  }

  const loadError = task.error ?? locations.error;
  return (
    <Stack
      gap='md'
      style={{
        fontVariantNumeric: 'tabular-nums',
        touchAction: 'manipulation',
        overflowWrap: 'anywhere'
      }}
    >
      <PageDetail
        title={t`Cycle count`}
        subtitle={id ? t`Count ${id}` : t`Start a blind location count`}
        badges={
          currentState
            ? [
                <Badge key='state' variant='light'>
                  {stateLabel(currentState)}
                </Badge>
              ]
            : []
        }
      />
      {(error || loadError) && (
        <Alert color='red' role='alert' title={t`Count action unavailable`}>
          {error || failureMessage(loadError)}
        </Alert>
      )}
      {notice && (
        <Text component='output' aria-live='polite'>
          {notice}
        </Text>
      )}
      <Group justify='space-between'>
        <Text
          size='sm'
          c='dimmed'
        >{t`Observations are saved as evidence. Only an approved, separately applied count changes stock.`}</Text>
        <Button
          variant='default'
          disabled={busy}
          loading={task.isFetching || locations.isFetching}
          onClick={() => void refresh()}
        >{t`Refresh saved state`}</Button>
      </Group>
      {(!id ? locations.isPending : task.isPending) && (
        <Stack
          component='output'
          aria-live='polite'
          aria-label={t`Loading count…`}
        >
          <Skeleton height={44} animate={false} />
          <Skeleton height={110} animate={false} />
        </Stack>
      )}
      {!id && locations.data && (
        <Paper withBorder p='md'>
          <form onSubmit={start}>
            <Stack>
              <Title order={2}>{t`Choose a stock location`}</Title>
              <Text>{t`Count every item in the selected location. This counting view hides recorded quantities; other stock permissions still apply. Serialized stock requires its separate workflow.`}</Text>
              {!locations.data.locations.length ? (
                <Text>{t`No count locations are available to your account. Ask your administrator.`}</Text>
              ) : (
                <>
                  <Select
                    label={t`Stock location`}
                    placeholder={t`Select a location…`}
                    name='count_location'
                    autoComplete='off'
                    searchable
                    required
                    value={location}
                    onChange={setLocation}
                    data={locations.data.locations.map((row) => ({
                      value: String(row.id),
                      label: row.name
                    }))}
                    disabled={blocked}
                  />
                  <Group>
                    <Button
                      type='submit'
                      loading={busy}
                      disabled={blocked || !location}
                    >{t`Start location count`}</Button>
                  </Group>
                </>
              )}
            </Stack>
          </form>
        </Paper>
      )}
      {observer && (
        <Stack>
          <Title order={2}>{observer.location.name}</Title>
          {terminal && (
            <Alert
              color='yellow'
              title={stateLabel(currentState)}
            >{t`The original observations and review are retained. Start a new count to request a new approval.`}</Alert>
          )}
          {applied && (
            <Alert
              color='green'
              title={t`Stock adjustment recorded`}
            >{t`This count has been applied. Refreshing or retrying its recorded command will not apply it twice.`}</Alert>
          )}
          {observer.state === 'REVIEW' && !terminal && !applied && (
            <Alert
              title={stateLabel(currentState)}
            >{t`Your observations are locked. The assigned independent reviewer must review the variance and explicitly apply an approved count.`}</Alert>
          )}
          {observer.state === 'OPEN' && (
            <Text>{t`Items remaining: ${pending}. Saved observations cannot be edited; corrections require a new count.`}</Text>
          )}
          {observer.items.map((item) => (
            <Paper
              key={item.id}
              withBorder
              p='md'
              style={{
                contentVisibility: 'auto',
                containIntrinsicSize: 'auto 160px'
              }}
            >
              <Stack gap='sm'>
                <ItemIdentity item={item} />
                {item.observed !== null ? (
                  <Text>
                    {t`Saved observed quantity`}:{' '}
                    <bdi translate='no'>
                      {item.observed} {item.unit}
                    </bdi>
                  </Text>
                ) : (
                  observer.state === 'OPEN' && (
                    <form
                      onSubmit={(event) => {
                        event.preventDefault();
                        void command(
                          `${endpoint}${id}/`,
                          {
                            item_id: item.id,
                            quantity: drafts[item.id] ?? '',
                            expected_revision: observer.revision
                          },
                          t`Observation saved. Stock quantities have not changed.`
                        );
                      }}
                    >
                      <Group align='end' grow>
                        <TextInput
                          ref={(element) => {
                            if (element) inputs.current.set(item.id, element);
                            else inputs.current.delete(item.id);
                          }}
                          label={t`Observed quantity`}
                          description={t`Enter the physical count in ${item.unit || t`item units`}, with at most five decimal places.`}
                          required
                          value={drafts[item.id] ?? ''}
                          onChange={(event) =>
                            setDrafts({
                              ...drafts,
                              [item.id]: event.currentTarget.value
                            })
                          }
                          name={`observed_${item.id}`}
                          autoComplete='off'
                          spellCheck={false}
                          inputMode='decimal'
                          maxLength={32}
                          error={
                            error && activeAction === item.id
                              ? error
                              : undefined
                          }
                          disabled={blocked}
                          styles={{ input: { direction: 'ltr' } }}
                        />
                        <Button
                          type='submit'
                          disabled={blocked || !drafts[item.id]}
                          loading={busy && activeAction === item.id}
                        >{t`Save observation`}</Button>
                      </Group>
                    </form>
                  )
                )}
              </Stack>
            </Paper>
          ))}
          {observer.state === 'OPEN' && (
            <Group>
              <Button
                ref={reviewButton}
                disabled={blocked || pending > 0}
                loading={busy && activeAction === 'REQUEST'}
                onClick={() =>
                  void command(
                    `${endpoint}${id}/request/`,
                    { expected_revision: observer.revision },
                    t`Independent review requested. Stock quantities have not changed.`
                  )
                }
              >{t`Request independent review`}</Button>
              <Text size='sm'>
                {pending > 0
                  ? t`Save an observation for every item first.`
                  : t`Requesting review locks these observations.`}
              </Text>
            </Group>
          )}
          {observer.review && (
            <Anchor
              component={Link}
              to={`/stock/cycle-count/${id}/review/`}
            >{t`Assigned reviewer link`}</Anchor>
          )}
          {(terminal || applied) && (
            <Anchor
              component={Link}
              to='/stock/cycle-count/'
            >{t`Start a new count`}</Anchor>
          )}
        </Stack>
      )}
      {review && (
        <Stack>
          <Title order={2}>{t`Independent variance review`}</Title>
          <Text fw={600}>
            {review.location.name} ·{' '}
            <bdi translate='no'>#{review.location.id}</bdi>
          </Text>
          <Text>
            {t`Policy`}: <bdi translate='no'>{review.policyVersion}</bdi> ·{' '}
            {t`Expires`}:{' '}
            <time dateTime={review.expiresAt}>
              <bdi>
                {i18n.date(new Date(review.expiresAt), {
                  year: 'numeric',
                  month: 'short',
                  day: 'numeric',
                  hour: 'numeric',
                  minute: '2-digit',
                  timeZoneName: 'short'
                })}
              </bdi>
            </time>
          </Text>
          {review.materialStale && (
            <Alert
              color='yellow'
              title={t`Stock or item identity changed`}
            >{t`These are the original frozen observations. Approval and application are blocked; request a new physical count.`}</Alert>
          )}
          {terminal && (
            <Alert
              color='yellow'
              title={stateLabel(currentState)}
            >{t`This review cannot be reused. The counter must start a new count.`}</Alert>
          )}
          {applied && (
            <Alert
              color='green'
              title={t`Stock adjustment recorded`}
            >{t`The approved count was applied once. Original count evidence remains available below.`}</Alert>
          )}
          {review.items.map((item) => (
            <Paper
              key={item.item}
              withBorder
              p='md'
              style={{
                contentVisibility: 'auto',
                containIntrinsicSize: 'auto 160px'
              }}
            >
              <Stack gap='sm'>
                <ItemIdentity item={item} />
                <Group grow>
                  <Text>
                    {t`Original quantity`}:{' '}
                    <bdi translate='no'>
                      {item.expected} {item.unit}
                    </bdi>
                  </Text>
                  <Text>
                    {t`Counted quantity`}:{' '}
                    <bdi translate='no'>
                      {item.counted} {item.unit}
                    </bdi>
                  </Text>
                  <Text fw={600}>
                    {t`Adjustment`}:{' '}
                    <bdi translate='no'>
                      {item.delta} {item.unit}
                    </bdi>
                  </Text>
                </Group>
              </Stack>
            </Paper>
          ))}
          {!terminal && !applied && !review.materialStale && (
            <Stack>
              {['PENDING', 'APPROVED'].includes(currentState) && (
                <TextInput
                  name='review_reason'
                  autoComplete='off'
                  label={t`Review reason`}
                  description={t`A reason is required to reject or revoke. Use one line, up to 255 characters.`}
                  value={reason}
                  onChange={(event) => setReason(event.currentTarget.value)}
                  maxLength={255}
                  disabled={blocked}
                />
              )}
              <Group>
                {currentState === 'PENDING' && (
                  <Button
                    loading={busy && activeAction === 'APPROVED'}
                    disabled={blocked}
                    onClick={() =>
                      void command(
                        `${endpoint}${id}/review/`,
                        {
                          expected_revision: review.approvalRevision,
                          decision: 'APPROVED',
                          reason
                        },
                        t`Count approved. Stock quantities have not changed.`
                      )
                    }
                  >{t`Approve count`}</Button>
                )}
                {currentState === 'PENDING' && (
                  <Button
                    variant='default'
                    disabled={blocked || !reason.trim()}
                    onClick={() =>
                      void command(
                        `${endpoint}${id}/review/`,
                        {
                          expected_revision: review.approvalRevision,
                          decision: 'REJECTED',
                          reason
                        },
                        t`Review rejected. Stock quantities have not changed.`
                      )
                    }
                  >{t`Reject review`}</Button>
                )}
                {currentState === 'APPROVED' && (
                  <Button
                    disabled={blocked}
                    onClick={() => {
                      setError('');
                      setConfirm(true);
                    }}
                  >{t`Apply approved count`}</Button>
                )}
                {currentState === 'APPROVED' && (
                  <Button
                    variant='default'
                    disabled={blocked || !reason.trim()}
                    onClick={() =>
                      void command(
                        `${endpoint}${id}/review/`,
                        {
                          expected_revision: review.approvalRevision,
                          decision: 'REVOKED',
                          reason
                        },
                        t`Approval revoked. Stock quantities have not changed.`
                      )
                    }
                  >{t`Revoke approval`}</Button>
                )}
              </Group>
            </Stack>
          )}
        </Stack>
      )}
      <Modal
        opened={confirm}
        onClose={() => {
          if (!busy) setConfirm(false);
        }}
        title={t`Apply this approved count?`}
        closeOnEscape={!busy}
        closeOnClickOutside={!busy}
        withCloseButton={!busy}
      >
        <Stack>
          {error && (
            <Alert color='red' role='alert'>
              {error}
            </Alert>
          )}
          {review && (
            <Text fw={600}>
              {review.location.name} ·{' '}
              <bdi translate='no'>#{review.location.id}</bdi>
            </Text>
          )}
          <Text>{t`This changes physical stock for the items listed below to their counted quantities. Approval alone did not change stock. A correction requires a new reviewed count.`}</Text>
          {review?.items.map((item) => (
            <Text key={item.item}>
              {item.partName} · {t`Stock item`}{' '}
              <bdi translate='no'>#{item.item}</bdi>:{' '}
              <bdi translate='no'>
                {item.expected} → {item.counted} {item.unit}
              </bdi>
            </Text>
          ))}
          <Group justify='flex-end'>
            <Button
              variant='default'
              disabled={blocked}
              onClick={() => setConfirm(false)}
            >{t`Cancel`}</Button>
            <Button
              loading={busy}
              disabled={blocked}
              onClick={() =>
                void command(
                  `${endpoint}${id}/commit/`,
                  {},
                  t`Approved stock adjustment recorded.`
                )
              }
            >{t`Apply stock adjustment`}</Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
