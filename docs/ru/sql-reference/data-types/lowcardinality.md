---
slug: /ru/sql-reference/data-types/lowcardinality
sidebar_position: 51
sidebar_label: LowCardinality
---

# LowCardinality {#lowcardinality-data-type}

Изменяет внутреннее представление других типов данных, превращая их в тип со словарным кодированием.

## Синтаксис {#lowcardinality-syntax}

```sql
LowCardinality(data_type)
```

**Параметры**

-   `data_type` — [String](string.md), [FixedString](fixedstring.md), [Date](date.md), [DateTime](datetime.md) и числа за исключением типа [Decimal](decimal.md). `LowCardinality` неэффективен для некоторых типов данных, см. описание настройки [allow_suspicious_low_cardinality_types](../../operations/settings/settings.md#allow_suspicious_low_cardinality_types).

## Описание {#lowcardinality-dscr}

`LowCardinality` — это надстройка, изменяющая способ хранения и правила обработки данных. ClickHouse применяет [словарное кодирование](https://en.wikipedia.org/wiki/Dictionary_coder) в столбцы типа `LowCardinality`. Работа с данными, представленными в словарном виде, может значительно увеличивать производительность запросов [SELECT](../statements/select/index.md) для многих приложений.

Эффективность использования типа данных `LowCardinality` зависит от разнообразия данных. Если словарь содержит менее 10 000 различных значений, ClickHouse в основном показывает более высокую эффективность чтения и хранения данных. Если же словарь содержит более 100 000 различных значений, ClickHouse может работать хуже, чем при использовании обычных типов данных.

При работе со строками использование `LowCardinality` вместо [Enum](enum.md) обеспечивает большую гибкость в использовании и часто показывает такую же или более высокую эффективность.

## Пример

Создание таблицы со столбцами типа `LowCardinality`:

```sql
CREATE TABLE lc_t
(
    `id` UInt16,
    `strings` LowCardinality(String)
)
ENGINE = MergeTree()
ORDER BY id
```

## Связанные настройки и функции

Настройки:

-   [low_cardinality_max_dictionary_size](../../operations/settings/settings.md#low_cardinality_max_dictionary_size)
-   [low_cardinality_use_single_dictionary_for_part](../../operations/settings/settings.md#low_cardinality_use_single_dictionary_for_part)
-   [low_cardinality_allow_in_native_format](../../operations/settings/settings.md#low_cardinality_allow_in_native_format)
-   [allow_suspicious_low_cardinality_types](../../operations/settings/settings.md#allow_suspicious_low_cardinality_types)
-   [output_format_arrow_low_cardinality_as_dictionary](/operations/settings/formats#output_format_arrow_low_cardinality_as_dictionary)

Функции:

-   [toLowCardinality](../functions/type-conversion-functions.md#tolowcardinality)

## Смотрите также

-   [Reducing ClickHouse Storage Cost with the Low Cardinality Type – Lessons from an Instana Engineer](https://www.instana.com/blog/reducing-clickhouse-storage-cost-with-the-low-cardinality-type-lessons-from-an-instana-engineer/).
-   [String Optimization (video presentation in Russian)](https://youtu.be/rqf-ILRgBdY?list=PL0Z2YDlm0b3iwXCpEFiOOYmwXzVmjJfEt). [Slides in English](https://github.com/ClickHouse/clickhouse-presentations/raw/master/meetup19/string_optimization.pdf).
