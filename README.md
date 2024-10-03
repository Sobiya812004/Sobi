collections = { }
def createCollection(p_collection_name):
    """Create an empty collection."""
    if p_collection_name in collections:
        return f"Collection {p_collection_name} already exists."
    else:
        collections[p_collection_name] = [ ]
        return f"Collection {p_collection_name} created."
def indexData(p_collection_name, p_exclude_column):
    """Indexes the employee data excluding a specific column."""
    collection = collections.get(p_collection_name)
    if not collection:
         return f"Collection{p_collection_name} does not exist."        
    indexed_data = [ ]
    for record in collection:
        indexed_record = {k: v for k, v in record.items() if k != p_exclude_column}
        indexed_data.append(indexed_record)
        return indexed_data

def searchByColumn(p_collection_name, p_column_name, p_column_value):
    """Search for employees by a specific column value."""
    if p_collection_name not in collections:
        return f"Collection {p_collection_name} does not exist."
    result = [employee for employee in collections[p_collection_name] if employee.get(p_column_name) == p_column_value]
    return result
    
def getEmpCount(p_collection_name):
    """Returns the total count of employees in the collection."""
    collection = collections.get(p_collection_name)
    if not collection:
        return f"Collection {p_collection_name} does not exist."
    return len(collection)


def delEmpById(p_collection_name, p_employee_id):
    """Deletes an employee by their ID."""
    if p_collection_name not in collections:
        return f"Collection {p_collection_name} does not exist."
        initial_count = len(collections[p_collection_name])
        collections[p_collection_name] = [emp for emp in collections[p_collection_name] if emp['employee_id'] != p_employee_id]
        if len(collections[p_collection_name]) < initial_count:
            return f"Employee with ID {p_employee_id} has been deleted."
        else:
            return f"Employee with ID {p_employee_id} not found."
def getDepFacet(p_collection_name):
    collection = collections.get(p_collection_name)
    if not collection:
        return f"Collection {p_collection_name} does not exist."
    department_count = { }
    for record in collection:
        department = record.get('department')
        if department in department_count:
            department_count[department] += 1
        else:
            department_count[department] = 1
        return department_count


# Function executions

v_nameCollection = 'HashJohnDoe'
v_phoneCollection = 'Hash 1234'
print(createCollection(v_nameCollection))
print(createCollection(v_phoneCollection))
collections[v_nameCollection] = [ {'employee_id': 'E02001', 'name': 'John Doe', 'department': 'HR', 'gender':'Male'},
                                {'employee_id': 'E02002', 'name': 'Jane Smith', 'department': 'IT', 'gender': 'Female'},
                                  {'employee_id': 'E02003', 'name': 'Alice Johnson', 'department': 'HR', 'gender': 'Female'} ]
collections[v_phoneCollection] = [  {'employee_id': 'P1234', 'name': 'Bob Brown', 'department': 'IT', 'gender': 'Male'},
                                   {'employee_id': 'P1235', 'name': 'Charlie Green', 'department': 'Finance', 'gender': 'Male'} ]
print(getEmpCount(v_nameCollection))
print(indexData(v_nameCollection, 'department'))
print(indexData(v_phoneCollection, 'gender'))
print(delEmpById(v_nameCollection, 'E02003'))
print(getEmpCount(v_nameCollection))
print(searchByColumn(v_nameCollection, 'department', 'IT'))
print(searchByColumn(v_nameCollection, 'gender', 'Male'))
print(searchByColumn(v_phoneCollection, 'department', 'IT'))
print(getDepFacet(v_nameCollection))
print(getDepFacet(v_phoneCollection))
        
