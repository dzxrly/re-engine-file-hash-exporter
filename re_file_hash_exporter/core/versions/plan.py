from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Iterable, Iterator

CancelCallback = Callable[[], bool]


def _cancelled(cancel_requested: CancelCallback | None) -> bool:
    return bool(cancel_requested and cancel_requested())


def _check_cancelled(cancel_requested: CancelCallback | None) -> None:
    if _cancelled(cancel_requested):
        raise InterruptedError("Version planning was cancelled.")


def _ordered_unique(values: Iterable[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for value in values:
        value = int(value)
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


@dataclass(slots=True)
class VersionPlan:
    description: str
    count: int
    low: int | None
    high: int | None
    _iter_values: Callable[[CancelCallback | None], Iterator[int]] = field(repr=False)
    _with_minimum: Callable[[int], "VersionPlan"] | None = field(default=None, repr=False)
    _contains: Callable[[int], bool] | None = field(default=None, repr=False)

    def iter_values(self, cancel_requested: CancelCallback | None = None) -> Iterator[int]:
        yield from self._iter_values(cancel_requested)

    def iter_chunks(
        self,
        chunk_size: int,
        cancel_requested: CancelCallback | None = None,
    ) -> Iterator[list[int]]:
        chunk: list[int] = []
        for value in self.iter_values(cancel_requested):
            chunk.append(value)
            if len(chunk) >= chunk_size:
                yield chunk
                chunk = []
        if chunk:
            yield chunk

    def with_minimum(self, minimum: int | None) -> "VersionPlan":
        if minimum is None:
            return self
        minimum = int(minimum)
        if self._with_minimum is not None:
            return self._with_minimum(minimum)

        def iter_values(cancel_requested: CancelCallback | None = None) -> Iterator[int]:
            for value in self.iter_values(cancel_requested):
                if value >= minimum:
                    yield value

        if self.high is not None and self.high < minimum:
            count = 0
            low = None
            high = None
        elif self.low is not None and self.low >= minimum:
            count = self.count
            low = self.low
            high = self.high
        else:
            count = sum(1 for _value in iter_values(None))
            low = minimum if count else None
            high = self.high

        return VersionPlan(
            description=f"{self.description}, >= {minimum}",
            count=count,
            low=low,
            high=high,
            _iter_values=iter_values,
            _contains=lambda value: self.contains(value) and int(value) >= minimum,
        )

    def contains(self, value: int) -> bool:
        value = int(value)
        if self._contains is not None:
            return self._contains(value)
        return any(candidate == value for candidate in self.iter_values(None))

    def without_values(self, excluded: Iterable[int], label: str = "already discovered") -> "VersionPlan":
        excluded_values = {int(value) for value in excluded}
        if not excluded_values or not self.count:
            return self

        matching_values = {value for value in excluded_values if self.contains(value)}
        if not matching_values:
            return self

        count = max(0, self.count - len(matching_values))
        description = f"{self.description}, excluding {len(matching_values)} {label}"
        if count <= 0:
            return empty_version_plan(description)

        def iter_values(cancel_requested: CancelCallback | None = None) -> Iterator[int]:
            for value in self.iter_values(cancel_requested):
                if value not in matching_values:
                    yield value

        def with_minimum(minimum: int) -> "VersionPlan":
            return self.with_minimum(minimum).without_values(matching_values, label)

        return VersionPlan(
            description=description,
            count=count,
            low=self.low,
            high=self.high,
            _iter_values=iter_values,
            _with_minimum=with_minimum,
            _contains=lambda value: self.contains(value) and int(value) not in matching_values,
        )


def empty_version_plan(description: str = "none") -> VersionPlan:
    return VersionPlan(
        description=description,
        count=0,
        low=None,
        high=None,
        _iter_values=lambda _cancel: iter(()),
        _contains=lambda _value: False,
    )


def numeric_range_plan(
    start: int,
    end: int,
    priority_versions: Iterable[int] = (),
    description: str = "numeric range",
) -> VersionPlan:
    start = max(0, int(start))
    end = max(start, int(end))
    priorities = [value for value in _ordered_unique(priority_versions) if start <= value <= end]
    priority_set = set(priorities)
    count = end - start + 1

    def iter_values(cancel_requested: CancelCallback | None = None) -> Iterator[int]:
        for value in priorities:
            _check_cancelled(cancel_requested)
            yield value
        for index, value in enumerate(range(start, end + 1), start=1):
            if index % 8192 == 0:
                _check_cancelled(cancel_requested)
            if value in priority_set:
                continue
            yield value

    def with_minimum(minimum: int) -> VersionPlan:
        if end < minimum:
            return empty_version_plan(f"{description}, >= {minimum}")
        return numeric_range_plan(
            max(start, minimum),
            end,
            priorities,
            f"{description}, >= {minimum}",
        )

    return VersionPlan(
        description=description,
        count=count,
        low=start,
        high=end,
        _iter_values=iter_values,
        _with_minimum=with_minimum,
        _contains=lambda value: start <= int(value) <= end,
    )


def discrete_version_plan(
    values: Iterable[int],
    description: str = "discrete versions",
) -> VersionPlan:
    ordered = _ordered_unique(values)
    value_set = set(ordered)
    low = min(ordered) if ordered else None
    high = max(ordered) if ordered else None

    def iter_values(cancel_requested: CancelCallback | None = None) -> Iterator[int]:
        for value in ordered:
            _check_cancelled(cancel_requested)
            yield value

    def with_minimum(minimum: int) -> VersionPlan:
        return discrete_version_plan(
            [value for value in ordered if value >= minimum],
            f"{description}, >= {minimum}",
        )

    return VersionPlan(
        description=description,
        count=len(ordered),
        low=low,
        high=high,
        _iter_values=iter_values,
        _with_minimum=with_minimum,
        _contains=lambda value: int(value) in value_set,
    )


def concatenated_version_plan(
    plans: Iterable[VersionPlan],
    description: str = "concatenated version plans",
) -> VersionPlan:
    active_plans = tuple(plan for plan in plans if plan.count)
    if not active_plans:
        return empty_version_plan(description)

    lows = [plan.low for plan in active_plans if plan.low is not None]
    highs = [plan.high for plan in active_plans if plan.high is not None]

    def iter_values(cancel_requested: CancelCallback | None = None) -> Iterator[int]:
        for plan in active_plans:
            yield from plan.iter_values(cancel_requested)

    def with_minimum(minimum: int) -> VersionPlan:
        return concatenated_version_plan(
            (plan.with_minimum(minimum) for plan in active_plans),
            f"{description}, >= {minimum}",
        )

    return VersionPlan(
        description=description,
        count=sum(plan.count for plan in active_plans),
        low=min(lows) if lows else None,
        high=max(highs) if highs else None,
        _iter_values=iter_values,
        _with_minimum=with_minimum,
        _contains=lambda value: any(plan.contains(value) for plan in active_plans),
    )


def date_code_plan(
    start_date: date,
    end_date: date,
    tail_width: int,
    priority_dates: Iterable[date] = (),
    priority_tails: Iterable[int] = (),
    description: str = "date_code range",
) -> VersionPlan:
    if end_date < start_date:
        start_date, end_date = end_date, start_date

    tail_count = 10**int(tail_width)
    tail_low = 0
    tail_high = tail_count - 1
    date_count = (end_date - start_date).days + 1
    count = date_count * tail_count

    all_dates = [date.fromordinal(ordinal) for ordinal in range(start_date.toordinal(), end_date.toordinal() + 1)]
    priority_date_values = [current for current in sorted(set(priority_dates)) if start_date <= current <= end_date]
    priority_date_set = set(priority_date_values)
    remainder_dates = [current for current in all_dates if current not in priority_date_set]

    priority_tail_values = [tail for tail in _ordered_unique(priority_tails) if tail_low <= tail <= tail_high]
    priority_tail_set = set(priority_tail_values)
    remainder_tails = [tail for tail in range(tail_count) if tail not in priority_tail_set]

    date_phases = [priority_date_values, remainder_dates] if priority_date_values else [all_dates]
    tail_phases = [priority_tail_values, remainder_tails] if priority_tail_values else [list(range(tail_count))]
    multiplier = 10**int(tail_width)
    low = int(start_date.strftime("%y%m%d")) * multiplier
    high = int(end_date.strftime("%y%m%d")) * multiplier + tail_high

    def iter_values(cancel_requested: CancelCallback | None = None) -> Iterator[int]:
        yielded = 0
        for dates in date_phases:
            for tails in tail_phases:
                for current in dates:
                    _check_cancelled(cancel_requested)
                    prefix = int(current.strftime("%y%m%d")) * multiplier
                    for tail in tails:
                        yielded += 1
                        if yielded % 8192 == 0:
                            _check_cancelled(cancel_requested)
                        yield prefix + tail

    def count_from_minimum(minimum: int) -> int:
        total = 0
        for current in all_dates:
            prefix = int(current.strftime("%y%m%d")) * multiplier
            if prefix + tail_high < minimum:
                continue
            if prefix >= minimum:
                total += tail_count
                continue
            first_tail = max(tail_low, minimum - prefix)
            if first_tail <= tail_high:
                total += tail_high - first_tail + 1
        return total

    def with_minimum(minimum: int) -> VersionPlan:
        if high < minimum:
            return empty_version_plan(f"{description}, >= {minimum}")
        filtered_count = count_from_minimum(minimum)

        def filtered_values(cancel_requested: CancelCallback | None = None) -> Iterator[int]:
            for value in iter_values(cancel_requested):
                if value >= minimum:
                    yield value

        return VersionPlan(
            description=f"{description}, >= {minimum}",
            count=filtered_count,
            low=max(low, minimum) if filtered_count else None,
            high=high if filtered_count else None,
            _iter_values=filtered_values,
            _contains=lambda value: contains(value) and int(value) >= minimum,
        )

    def contains(value: int) -> bool:
        value = int(value)
        if value < low or value > high:
            return False
        date_value, tail = divmod(value, multiplier)
        if tail < tail_low or tail > tail_high:
            return False
        try:
            current = datetime.strptime(f"{date_value:06d}", "%y%m%d").date()
        except ValueError:
            return False
        return start_date <= current <= end_date

    return VersionPlan(
        description=description,
        count=count,
        low=low,
        high=high,
        _iter_values=iter_values,
        _with_minimum=with_minimum,
        _contains=contains,
    )
