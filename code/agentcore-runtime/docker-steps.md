Use this workflow whenever you change the application code, skills, or dependencies. Run the commands in **PowerShell**, one step at a time.

### 1. Open the application folder

Open the folder containing your `Dockerfile`, `main.py`, `requirements.txt`, and `skills/`.

Ensure `.dockerignore` excludes `.env`, credentials, and local session files.

### 2. Set the deployment values

Replace the placeholders. Use a **new image tag for each release**.

```powershell
$ImageName = "agentcore-runtime"
$ImageTag = "{enter incremental new image tag}"
$RepositoryName = "agentcore-runtime"
$AwsRegion = "<aws-region>"
$LocalImage = "${ImageName}:${ImageTag}"
```

### 3. Build the image

Start Docker Desktop, then run:

```powershell
docker buildx build --platform linux/arm64 --provenance=false --load -t $LocalImage .
```

The final `.` builds from the current folder, including your updated code.

### 4. Verify the architecture

```powershell
docker image inspect $LocalImage --format '{{.Os}}/{{.Architecture}}'
```

Expected output:

```text
linux/arm64
```

### 5. Authenticate to AWS

For a named SSO profile:

```powershell
$env:AWS_PROFILE = "<sso-profile>"
aws sso login
```

Skip `aws sso login` if the session is still valid.

### 6. Create the ECR repository

**Run this only if the repository does not exist:**

```powershell
aws ecr create-repository --repository-name $RepositoryName --region $AwsRegion
```

**If it already exists, skip creation.** Retrieve its URI in either case:

```powershell
$RepositoryUri = aws ecr describe-repositories --repository-names $RepositoryName --region $AwsRegion --query 'repositories[0].repositoryUri' --output text
$Registry = $RepositoryUri.Split('/')[0]
$ImageUri = "${RepositoryUri}:${ImageTag}"
```

### 7. Log Docker into ECR

```powershell
aws ecr get-login-password --region $AwsRegion | docker login --username AWS --password-stdin $Registry
```

Expect `Login Succeeded`. `AWS` is the required literal username.

### 8. Tag and push the new image

```powershell
docker tag $LocalImage $ImageUri
docker push $ImageUri
```

This uploads the image built in step 3 with the new release tag.

### 9. Verify the upload

```powershell
aws ecr describe-images --repository-name $RepositoryName --image-ids "imageTag=$ImageTag" --region $AwsRegion --query 'imageDetails[0].{Tags:imageTags,Digest:imageDigest}' --output table
```

Print the deployment URI:

```powershell
$ImageUri
```

Use that URI in your AgentCore runtime configuration. **Pushing to ECR does not automatically update the runtime.**