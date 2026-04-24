# Contributing to Terrahawk

Thank you for your interest in contributing to Terrahawk! This document provides guidelines for contributing to the project.

## Getting Started

1. **Fork the repository** on GitHub
2. **Clone your fork** locally:

   ```bash
   git clone https://github.com/YOUR-USERNAME/terrahawk.git
   cd terrahawk
   ```

3. **Create a branch** for your changes:

   ```bash
   git checkout -b feature/your-feature-name
   ```

4. **Set up the development environment**:

   ```bash
   # Install as editable package
   pip install -e .

   # Verify it works
   python3 terrahawk.py --version
   python3 -m terrahawk --version
   ```

## Types of Contributions

### 1. Bug Reports

If you find a bug, please create an issue with:

- Clear title describing the problem
- Steps to reproduce
- Expected vs actual behaviour
- Your environment (OS, Python version, Terraform/Terragrunt versions)
- Any error messages or logs
- The cloud backend in use (AWS, Azure, GCP)

### 2. Feature Requests

For new features:

- Explain the use case and problem it solves
- Describe the proposed solution
- Consider impact on all three cloud backends (AWS, Azure, GCP)
- Discuss alternatives you've considered

### 3. Documentation Improvements

Documentation contributions are highly valued:

- Fix typos or clarify existing guides
- Add examples or use cases
- Improve CLI help descriptions
- Expand the README with new sections

### 4. Cloud Backend Support

To add or improve a cloud backend:

1. **Update `src/terrahawk/state_age.py`** with the new backend logic
2. **Follow existing patterns** — see `_query_gcs_blob_dates` for the most complete reference implementation
3. **Add CLI availability checks** using `shutil.which()`
4. **Include diagnostic output** so users can see why queries fail
5. **Update the Dockerfile** with any new CLI dependencies
6. **Test with a real backend** before submitting

### 5. Report Template

The HTML report template lives at `src/terrahawk/templates/report.html`. When modifying it:

- Keep it fully self-contained (no external dependencies except Mermaid CDN)
- Maintain dark/light theme support
- Test with both small (< 10 units) and large (100+ units) datasets
- Ensure mobile responsiveness

### 6. Code Improvements

For Python modules:

- Follow existing code style
- Keep the import graph acyclic (see [Project Structure](#project-structure))
- Add comments explaining complex logic
- Test thoroughly before submitting
- Update relevant documentation

## Project Structure

```
terrahawk/
├── terrahawk.py                 # Thin shim (entrypoint)
├── src/terrahawk/
│   ├── __init__.py              # __version__, re-export main
│   ├── __main__.py              # python -m terrahawk
│   ├── cli.py                   # Main pipeline orchestration
│   ├── config.py                # Config file loading, config dir detection
│   ├── deps.py                  # Dependency checking, version detection
│   ├── discovery.py             # Unit discovery, DAG builder
│   ├── incremental.py           # Manifest hashing for incremental mode
│   ├── worker.py                # Plan execution worker
│   ├── plan_parser.py           # Terraform plan output parser
│   ├── process.py               # Result processing (imports plan_parser)
│   ├── state_age.py             # Remote state age queries (Azure/AWS/GCS)
│   ├── report.py                # HTML report generation
│   └── templates/
│       ├── report.html          # HTML report template
│       ├── eagle.svg            # Logo (light theme)
│       └── eagle-white.svg      # Logo (dark theme)
├── Dockerfile                   # Multi-cloud Docker images
├── pyproject.toml               # Package metadata
└── THIRD_PARTY_LICENSES         # Licenses for bundled/invoked tools
```

### Import Graph (strict DAG, no cycles)

```
cli.py ──→ config.py, deps.py, discovery.py, incremental.py,
           worker.py, process.py, state_age.py, report.py
worker.py ──→ deps.py (mise_cmd helper)
process.py ──→ plan_parser.py
(all other modules: only stdlib)
```

## Coding Standards

### Python Style

- Target Python 3.9+ compatibility
- Use only the standard library — no third-party runtime dependencies
- Keep functions focused and single-purpose
- Use type hints sparingly — only where they meaningfully aid comprehension
- Prefer clarity over cleverness

### Commit Messages

Follow conventional commits:

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

Types:

- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `refactor`: Code refactoring
- `chore`: Maintenance tasks

Examples:

```
feat(state_age): add support for Terraform Cloud backend

fix(worker): handle terragrunt init timeout on locked state

docs(readme): add CI/CD examples for GitLab CI
```

## Pull Request Process

1. **Update documentation** for any user-facing changes
2. **Test your changes**:
   - Run `python3 terrahawk.py --help` and `--version`
   - Run `python3 -m terrahawk --help`
   - If modifying Docker: build and test at least one cloud variant
   - If modifying state age: test against a real backend
3. **Create pull request** with:
   - Clear title following commit message convention
   - Description of changes and motivation
   - Reference any related issues
   - Note which cloud backends were tested
4. **Code review**:
   - Address reviewer feedback
   - Keep discussions focused and professional
   - Be patient — maintainers review when available

## Testing

Before submitting, verify at minimum:

1. **Basic CLI**:

   ```bash
   python3 terrahawk.py --version
   python3 terrahawk.py --help
   python3 -m terrahawk --version
   ```

2. **Docker build** (if Dockerfile was modified):

   ```bash
   docker build --build-arg CLOUD=aws   -t terrahawk:aws   .
   docker build --build-arg CLOUD=azure -t terrahawk:azure .
   docker build --build-arg CLOUD=gcp   -t terrahawk:gcp   .
   ```

3. **Module imports**:

   ```bash
   python3 -c "
   import sys; sys.path.insert(0, 'src')
   from terrahawk import cli, config, deps, discovery, incremental
   from terrahawk import worker, plan_parser, process, state_age, report
   print('All modules import successfully')
   "
   ```

## Questions?

- **Issues**: Use GitHub Issues for bug reports and feature requests
- **Discussions**: Use GitHub Discussions for questions and ideas

## Code of Conduct

- Be respectful and inclusive
- Focus on constructive feedback
- Assume good intentions
- Help others learn and grow

## License

By contributing, you agree that your contributions will be licensed under the same [MIT License](LICENSE) that covers the project.

---

Thank you for helping improve Terrahawk!
