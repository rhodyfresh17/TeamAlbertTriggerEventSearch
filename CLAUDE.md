# CLAUDE.md - AI Assistant Guidelines for TriggerEventSearch

This document provides essential context and guidelines for AI assistants working on the TriggerEventSearch repository.

## Project Overview

**TriggerEventSearch** is a project for searching and managing trigger events. This repository is currently in its initial setup phase.

### Repository Status
- **Current State**: New/Empty repository - ready for initial development
- **Primary Purpose**: Event trigger search functionality (to be implemented)

## Repository Structure

```
TriggerEventSearch/
├── CLAUDE.md          # AI assistant guidelines (this file)
├── README.md          # Project documentation (to be created)
├── src/               # Source code (to be created)
├── tests/             # Test files (to be created)
└── docs/              # Documentation (to be created)
```

## Development Guidelines

### Code Style Conventions

1. **Language**: Determine based on project requirements (JavaScript/TypeScript, Python, Go, etc.)
2. **Formatting**: Use consistent formatting tools (Prettier, Black, gofmt, etc.)
3. **Naming Conventions**:
   - Use descriptive, meaningful names
   - Follow language-specific conventions (camelCase, snake_case, PascalCase as appropriate)

### Commit Message Format

Use conventional commits format:
```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

Types:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `style`: Code style changes (formatting, etc.)
- `refactor`: Code refactoring
- `test`: Adding or updating tests
- `chore`: Maintenance tasks

### Branch Naming

- Feature branches: `feature/<description>`
- Bug fixes: `fix/<description>`
- Claude AI branches: `claude/<session-id>`

## AI Assistant Instructions

### When Working on This Repository

1. **Read Before Modifying**: Always read existing files before making changes
2. **Minimal Changes**: Make only the changes necessary to complete the task
3. **Test Your Changes**: Ensure any code changes are tested appropriately
4. **Document Significant Changes**: Update documentation when adding major features

### Key Commands

```bash
# Check repository status
git status

# Run tests (once implemented)
# npm test / pytest / go test ./... (depending on language)

# Build project (once implemented)
# npm run build / python setup.py build / go build (depending on language)
```

### Development Workflow

1. Create or checkout the appropriate branch
2. Make changes incrementally
3. Test changes before committing
4. Write clear commit messages
5. Push changes to the designated branch

## Project-Specific Notes

### Trigger Event Search Concepts

This project focuses on:
- **Event Triggers**: Conditions or actions that initiate events
- **Search Functionality**: Querying and filtering trigger events
- **Event Management**: Creating, updating, and monitoring triggers

### Future Development Areas

- [ ] Define core data models for trigger events
- [ ] Implement search/query functionality
- [ ] Build API endpoints for event management
- [ ] Create user interface components
- [ ] Add comprehensive test coverage

## Getting Started

### Prerequisites

(To be defined based on technology stack)

### Setup Instructions

1. Clone the repository
2. Install dependencies (once defined)
3. Configure environment (once defined)
4. Run the application (once defined)

## Contributing

1. Always work on feature branches or designated AI branches
2. Follow the coding standards outlined above
3. Include tests for new functionality
4. Update documentation as needed

## Additional Resources

- Project issue tracker: GitHub Issues
- Documentation: `/docs` directory (to be created)

---

*This CLAUDE.md file should be updated as the project evolves and more conventions are established.*
