# Preview Architecture

이 문서는 현재 저장소 기준 preview 환경 구조를 `k3s`와 `GitHub Actions workflow` 중심으로 정리한 문서입니다.

현재 기준 핵심 아이디어는 아래와 같습니다.

- PR마다 `preview-pr-<번호>` namespace를 하나씩 사용합니다.
- GitHub-hosted runner가 Docker 이미지를 빌드해서 Docker Hub에 push합니다.
- 이후 GitHub Actions가 AWS SSM으로 private k3s 서버에 명령을 보내 `kubectl apply/delete`를 수행합니다.
- DB를 사용하는 앱이면 `preview-system` namespace의 공통 secret을 읽어 PR namespace용 secret과 DB를 준비합니다.
- PR이 닫히면 preview namespace와 PR 전용 DB를 정리합니다.
- 오래 남은 preview는 CronJob이 annotation 기준으로 정리합니다.

## 1. 전체 흐름

```text
GitHub PR opened / synchronize / reopened
  -> GitHub Actions
  -> Docker image build
  -> Docker Hub push
  -> PR 번호 기준 값 계산
  -> AWS SSM SendCommand
  -> private k3s server
  -> kubectl apply
  -> preview-pr-<번호> namespace 갱신
```

외부 접속까지 포함하면 흐름은 아래와 같습니다.

```text
Browser
  -> public ALB
  -> k3s 기본 ingress controller(Traefik)
  -> Ingress host rule
  -> preview-app Service
  -> preview-app Pod
```

현재 preview host 형식은 아래를 기준으로 사용합니다.

- `pr-<번호>.ajasu.kro.kr`

예:

- PR 4 -> `pr-4.ajasu.kro.kr`

## 2. 현재 디렉터리 역할

현재 preview 관련 파일은 크게 두 묶음으로 나뉩니다.

### 2-1. bootstrap 리소스

위치:

- `k3s/bootstrap/preview-system-secrets.example.yaml`
- `k3s/bootstrap/cleanup-expired-preview-cronjob.yaml`

이 리소스들은 클러스터에 한 번 먼저 넣어두는 성격입니다.

- `preview-system` namespace
- 공통 DB secret
- 만료 cleanup CronJob

즉 workflow가 매 PR마다 새로 만드는 것이 아니라, bootstrap 단계에서 미리 준비해두는 리소스입니다.

### 2-2. runtime 템플릿

위치:

- `k3s/runtime/preview-app-example.yaml`
- `k3s/runtime/preview-ingress.yaml`
- `k3s/runtime/preview-db-bootstrap-job.yaml`
- `k3s/runtime/preview-db-cleanup-job.yaml`

이 파일들은 클러스터에 미리 적용하는 용도가 아닙니다.

GitHub Actions가 `envsubst`로 필요한 값만 치환한 뒤, SSM을 통해 k3s 서버에서 `kubectl apply` 하는 템플릿입니다.

## 3. deploy workflow 역할

파일:

- `.github/workflows/preview-deploy.yml`

이 workflow는 PR `opened`, `synchronize`, `reopened` 시점에 동작합니다.

현재 기준으로 workflow가 계산하는 주요 값은 아래입니다.

- `PR_NUMBER`
- `NAMESPACE=preview-pr-<번호>`
- `DB_NAME=pr_<번호>`
- `PREVIEW_HOST=pr-<번호>.ajasu.kro.kr`
- `IMAGE_TAG=pr-<번호>-<short sha>`
- `EXPIRES_AT_UTC`
- `EXPIRES_AT_KST`

예를 들어 PR 번호가 `4`면:

- namespace: `preview-pr-4`
- db name: `pr_4`
- host: `pr-4.ajasu.kro.kr`

그 다음 workflow는 아래 순서로 진행합니다.

1. `app/` 기준으로 Docker image build
2. Docker Hub push
3. AWS credentials 설정
4. `tag:Name=EaaS-K3s-Server` 기준으로 배포 대상 EC2 조회
5. SSM으로 원격 shell script 실행
6. k3s에서 namespace / secret / job / app / ingress 적용
7. PR 댓글로 URL, namespace, 만료 시각 기록

## 4. k3s 서버 안에서 deploy 시 실제로 하는 일

`preview-deploy.yml`이 SSM으로 보내는 원격 스크립트는 대략 아래 순서로 동작합니다.

### 4-1. namespace 준비

먼저 PR namespace를 생성하거나 유지합니다.

- `preview-pr-<번호>`

그리고 namespace에 아래 metadata를 붙입니다.

- label
  - `preview-environment=true`
  - `pr-number=<번호>`
  - `managed-by=github-actions`
- annotation
  - `preview.example.com/expires-at=<UTC>`
  - `preview.example.com/expires-at-kst=<KST>`

이 annotation이 나중에 cleanup CronJob의 기준이 됩니다.

### 4-2. 공통 secret -> PR namespace secret 복사

`preview-system` namespace에는 공통 secret이 있어야 합니다.

- `preview-db-app-template`
- `preview-db-admin`

workflow는 여기서 값을 읽어 PR namespace에 아래 secret을 만듭니다.

- `app-db-secret`
- `db-admin-secret`

이 단계에서 필요한 값은 아래입니다.

`preview-db-app-template`

- `DB_HOST`
- `DB_PORT`
- `DB_USER`
- `DB_PASSWORD`

`preview-db-admin`

- `DB_HOST`
- `DB_PORT`
- `DB_ADMIN_USER`
- `DB_ADMIN_PASSWORD`

### 4-3. PR 전용 DB 준비

앱이 DB를 사용하는 경우 `preview-db-bootstrap` Job을 실행합니다.

역할:

- `pr_<번호>` database 생성
- 앱 계정 생성 또는 보장
- 해당 DB에 권한 부여

즉 PR 4라면 `pr_4` DB가 준비됩니다.

### 4-4. 앱 배포

그 다음 `preview-app` Deployment와 Service를 적용합니다.

현재 구조는:

- container image: `${IMAGE_REPOSITORY}:${IMAGE_TAG}`
- Service port: `80`
- Pod app port: `8000`

중요한 점:

앱 코드는 `DATABASE_URL`만 읽고, 이 값은 Pod env에서 조합해서 넣습니다.

즉 `preview-app-example.yaml` 안에서 아래 값들을 secret에서 받아:

- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`

최종적으로 Pod에 아래 env를 주입합니다.

- `DATABASE_URL=mysql://$(DB_USER):$(DB_PASSWORD)@$(DB_HOST):$(DB_PORT)/$(DB_NAME)`

즉 앱 코드가 DB 접속 문자열을 직접 조합하는 것이 아니라, manifest 쪽에서 `DATABASE_URL`을 만들어서 넘깁니다.

### 4-5. Ingress 적용

마지막으로 `preview-ingress.yaml`을 적용합니다.

Ingress의 host rule은 아래 형태입니다.

- `pr-<번호>.ajasu.kro.kr`

즉 PR 번호가 host 이름과 1:1로 연결됩니다.

## 5. delete workflow 역할

파일:

- `.github/workflows/preview-delete.yml`

이 workflow는 PR `closed` 시점에 동작합니다.

동작 순서는 아래와 같습니다.

1. PR 번호로 `NAMESPACE`, `DB_NAME` 계산
2. AWS SSM으로 k3s 서버에 cleanup script 전달
3. `preview-db-cleanup` Job 실행
4. `pr_<번호>` DB 삭제
5. `preview-pr-<번호>` namespace 삭제
6. PR 댓글에 닫힌 시각 기록

즉 PR 4가 닫히면:

- `preview-pr-4` 삭제
- `pr_4` DB 삭제 시도

## 6. TTL cleanup CronJob 역할

파일:

- `k3s/bootstrap/cleanup-expired-preview-cronjob.yaml`

이 CronJob은 예외 상황을 정리하는 안전장치입니다.

예:

- PR close workflow가 실패한 경우
- 사람이 수동으로 닫지 않고 방치한 경우
- 테스트 후 cleanup이 누락된 경우

CronJob 기준:

- `preview-environment=true` 라벨이 붙은 namespace만 대상
- `preview.example.com/expires-at` annotation 기준으로 만료 판단
- 만료되면 namespace 삭제
- 가능하면 `pr_<번호>` DB도 같이 삭제

현재 스케줄은 `5분마다`입니다.

즉 실제 삭제 시각은 “정확히 만료 시각”이 아니라, 만료 시각 이후 다음 Cron 실행 타이밍이 됩니다.

예를 들어:

- expires-at = `10:00`
- Cron schedule = 5분마다

그러면 실제 정리는 `10:00 ~ 10:05` 사이 다음 실행 때 일어날 수 있습니다.

## 7. 외부 진입 구조

현재는 k3s 서버가 private subnet에 있으므로, 브라우저에서 보려면 public entrypoint가 하나 필요합니다.

현재 기준 권장 구조는 아래입니다.

```text
Internet
  -> public ALB
  -> target group (Instance, port 80)
  -> EaaS-K3s-Server
  -> k3s 기본 Traefik
  -> Ingress host rule
  -> preview-app Service
  -> preview-app Pod
```

초기 설정 포인트:

- ALB는 `internet-facing`
- target group type은 `Instance`
- target은 `EaaS-K3s-Server`
- health check path는 `/`
- success code는 `200-404`

`/ping` bootstrap을 따로 하지 않기 때문에, 기본 ingress controller의 `404`도 정상 응답으로 간주하는 구조입니다.

## 8. 지금 구조에서 중요한 고정 규칙

현재 preview 플랫폼은 아래 규칙을 기준으로 동작합니다.

### 8-1. PR 번호가 환경 식별자다

동일 PR이면 항상 같은 이름을 씁니다.

- namespace: `preview-pr-<번호>`
- db: `pr_<번호>`
- host: `pr-<번호>.ajasu.kro.kr`

즉 같은 PR에 새 커밋이 여러 번 들어와도, 새로운 환경을 무한히 만드는 것이 아니라 같은 preview 환경을 갱신합니다.

### 8-2. DB는 PR 단위다

현재 구조는 deploy 때마다 DB를 새 이름으로 만들지 않습니다.

같은 PR이면 같은 `pr_<번호>` DB를 재사용합니다.

즉 앱 배포는 갱신되지만, DB 데이터는 유지될 수 있습니다.

### 8-3. DB를 쓰지 않는 앱이면 구조를 단순화할 수 있다

앱이 DB를 전혀 쓰지 않는다면 아래는 생략 가능합니다.

- `preview-system` namespace
- `preview-db-app-template`
- `preview-db-admin`
- `preview-db-bootstrap-job`
- `preview-db-cleanup-job`

그 경우 preview 구조는:

- namespace 생성
- app deploy
- ingress deploy
- PR close 시 namespace 삭제

정도로 단순화할 수 있습니다.

## 9. 현재 repo에서 중요한 파일

- `.github/workflows/preview-deploy.yml`
- `.github/workflows/preview-delete.yml`
- `k3s/runtime/preview-app-example.yaml`
- `k3s/runtime/preview-ingress.yaml`
- `k3s/runtime/preview-db-bootstrap-job.yaml`
- `k3s/runtime/preview-db-cleanup-job.yaml`
- `k3s/bootstrap/preview-system-secrets.example.yaml`
- `k3s/bootstrap/cleanup-expired-preview-cronjob.yaml`

## 10. 한 줄 요약

현재 구조는 아래 한 문장으로 요약할 수 있습니다.

`GitHub Actions가 PR 번호를 기준으로 preview 값을 계산하고, AWS SSM을 통해 private k3s 서버에서 PR 전용 namespace, DB, app, ingress를 생성/갱신/삭제하는 구조`
