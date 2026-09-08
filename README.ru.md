# opencode-postgres-readonly

[English](README.md) · **Русский**

Read-only PostgreSQL инструменты для OpenCode: выполнение безопасных
`SELECT`-запросов и список доступных проектов/окружений.

## Возможности

- **`database_exec`** — выполнение read-only SQL-запроса к PostgreSQL.
- **`database_list`** — список database-проектов и их окружений.

Инструменты полностью автономны: не используют общий код и не зависят от
внешних файлов учётных данных. Вся конфигурация хранится локально в
`config.json` рядом с исходными файлами.

## Установка

Скопируйте содержимое репозитория в каталог инструментов OpenCode:

```bash
# Создайте каталог инструментов, если его нет
mkdir -p ~/.config/opencode/tools/opencode-postgres-readonly

# Скопируйте исходные файлы и шаблон конфигурации (без локального config.json)
cp -r database.ts config.ts config.example.json db requirements.txt \
  ~/.config/opencode/tools/opencode-postgres-readonly/
```

Затем установите Python-зависимость:

```bash
pip install -r requirements.txt
# или
pip install "psycopg2-binary>=2.9"
```

### Создание локальной конфигурации

Скопируйте шаблон в `config.json` в каталоге установки и заполните реальными
данными:

```bash
cd ~/.config/opencode/tools/opencode-postgres-readonly
cp config.example.json config.json
chmod 600 config.json
```

### Перезапуск OpenCode

После установки завершите текущую сессию OpenCode и запустите её снова
(`opencode`), чтобы новые инструменты подхватились.

## Конфигурация

`config.json` намеренно игнорируется Git. Структура:

```json
{
  "projects": {
    "example-db": {
      "dev": {
        "driver": "postgres",
        "host": "db.example.invalid",
        "port": 5432,
        "database": "exampledb",
        "sslmode": "prefer",
        "auth": {
          "user": "reader",
          "password": "replace_with_real_password"
        }
      }
    }
  }
}
```

| Поле | Обязательное | Описание |
|------|:-----------:|----------|
| `driver` | да | Тип БД (пока только `postgres`) |
| `host` | да | Хост базы данных |
| `port` | да | Порт |
| `database` | да | Имя базы данных |
| `sslmode` | да | Режим SSL: `prefer`, `require`, `disable`, `verify-full` |
| `auth.user` | да | Пользователь БД |
| `auth.password` | да | Пароль |

Вложенность: `projects.<project>.<environment>`. У одного проекта может быть
несколько окружений (`dev`, `staging`, `prod`). Если `environment` не указан,
используется первое окружение из списка. Конфигурация читается один раз и
кэшируется в памяти на время жизни процесса.

> **Важно:** `config.json` не коммитится (в `.gitignore`). Права доступа —
> `chmod 600`.

## Использование

### database_exec

Выполняет read-only SQL-запрос:

```
database_exec({ project: "example-db", environment: "dev", query: "SELECT id, name FROM products LIMIT 5" })
```

Аргументы:

| Аргумент | Обязательный | Описание |
|----------|:-----------:|----------|
| `project` | да | Имя проекта из `config.json` |
| `environment` | нет | Имя окружения; по умолчанию — первое |
| `query` | да | Один read-only SQL-запрос |

Успешный ответ:

```json
{
  "columns": ["id", "name"],
  "rows": [[1, "Товар А"], [2, "Товар Б"]],
  "row_count": 2
}
```

Ошибки:

```json
{"error": "Query execution failed", "reason": "..."}
{"error": "Connection failed", "reason": "..."}
```

### database_list

Список проектов и окружений:

```
database_list()
```

Ответ:

```json
{
  "example-db": ["dev", "prod"]
}
```

## Безопасность и ограничения

- Разрешены только `SELECT`, `WITH`, `EXPLAIN` в качестве первого ключевого
  слова.
- Заблокированы все DDL/DML (`CREATE`, `DROP`, `INSERT`, `UPDATE`, `DELETE`
  и т.д.).
- Запрещены системные схемы (`pg_catalog`, `information_schema`, `pg_toast`,
  `pg_temp`).
- Запрещены опасные функции (`pg_terminate_backend`, `pg_sleep`, `lo_*`,
  `dblink` и т.д.).
- Заблокированы все идентификаторы с префиксом `pg_*`.
- Серверная транзакция `READ ONLY`.
- `search_path` принудительно устанавливается в `public`.
- Маскирование ошибок — реальная причина не уходит в stdout.
- Максимальная длина запроса — 10 000 символов.
- Разрешён только один SQL-запрос.
- Обратный слэш (`\`) отклоняется.
- Учётные данные хранятся только в локальном `config.json` (не коммитится).

## Разрешения в opencode.jsonc

Минимальный фрагмент для разрешения инструментов:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "permission": {
    "database_exec": "allow",
    "database_list": "allow"
  }
}
```

Для агента со строгими правами (`"*": "deny"`):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "agent": {
    "db-reader": {
      "mode": "subagent",
      "description": "Read-only database access.",
      "permission": {
        "*": "deny",
        "database_exec": "allow",
        "database_list": "allow"
      }
    }
  }
}
```

## Лицензия

[MIT](LICENSE)
